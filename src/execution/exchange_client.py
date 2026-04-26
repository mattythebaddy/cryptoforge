"""Production-grade CCXT exchange wrapper with retry and rate limiting."""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.pro
import structlog

from src.core.config import ExchangeConfig
from src.core.exceptions import (
    ExchangeError,
    InsufficientBalance,
    OrderRejected,
    RateLimitError,
)
from src.utils.retry import exchange_retry

log = structlog.get_logger(__name__)

# Cache balance for at most 30s
_BALANCE_CACHE_TTL = 30.0


class ExchangeClient:
    """Wraps CCXT with retry logic, rate limiting, and error handling."""

    def __init__(self, config: ExchangeConfig) -> None:
        self._config = config
        self._exchange: Any = None
        self._balance_cache: dict[str, Any] | None = None
        self._balance_ts: float = 0

    async def connect(self) -> None:
        """Initialize and connect to the exchange."""
        exchange_class = getattr(ccxt.pro, self._config.name, None)
        if exchange_class is None:
            raise ExchangeError(f"Exchange '{self._config.name}' not found in ccxt.pro")

        opts: dict[str, Any] = {
            "apiKey": self._config.api_key or None,
            "secret": self._config.api_secret or None,
            "enableRateLimit": self._config.rate_limit,
            "timeout": self._config.timeout,
            "options": {**self._config.options},
        }
        if self._config.testnet:
            opts["sandbox"] = True

        self._exchange = exchange_class(opts)
        await self._exchange.load_markets()
        log.info(
            "exchange.connected",
            name=self._config.name,
            testnet=self._config.testnet,
            markets=len(self._exchange.markets),
        )

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
            log.info("exchange.closed")

    @property
    def exchange(self) -> Any:
        if self._exchange is None:
            raise ExchangeError("Exchange not connected — call connect() first")
        return self._exchange

    # -- order placement --

    @exchange_retry(max_attempts=3)
    async def place_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place order with pre-flight validation and retry."""
        params = params or {}
        ex = self.exchange

        # Pre-flight checks
        market = ex.market(symbol)
        amount = self._round_amount(amount, market)
        if price is not None:
            price = self._round_price(price, market)

        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
        if amount < min_amount:
            raise OrderRejected(
                f"Amount {amount} below minimum {min_amount}",
                {"symbol": symbol, "amount": amount},
            )

        min_cost = market.get("limits", {}).get("cost", {}).get("min", 0)
        if price and amount * price < min_cost:
            raise OrderRejected(
                f"Notional {amount * price:.2f} below minimum {min_cost}",
                {"symbol": symbol},
            )

        try:
            order = await ex.create_order(symbol, order_type, side, amount, price, params)
            log.info(
                "order.placed",
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                order_id=order.get("id"),
            )
            return order
        except ccxt.InsufficientFunds as e:
            raise InsufficientBalance(str(e)) from e
        except ccxt.InvalidOrder as e:
            raise OrderRejected(str(e), {"symbol": symbol}) from e
        except ccxt.RateLimitExceeded as e:
            raise RateLimitError(retry_after=1.0) from e
        except ccxt.ExchangeError as e:
            raise ExchangeError(str(e)) from e

    # -- queries --

    async def get_balance(self, force: bool = False) -> dict[str, Any]:
        now = asyncio.get_event_loop().time()
        if not force and self._balance_cache and (now - self._balance_ts) < _BALANCE_CACHE_TTL:
            return self._balance_cache
        self._balance_cache = await self.exchange.fetch_balance()
        self._balance_ts = now
        return self._balance_cache

    async def get_free_balance(self, currency: str = "USDT") -> float:
        bal = await self.get_balance()
        return float(bal.get("free", {}).get(currency, 0))

    async def get_total_equity(self, currency: str = "USDT") -> float:
        bal = await self.get_balance()
        return float(bal.get("total", {}).get(currency, 0))

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            return await self.exchange.fetch_open_orders(symbol)
        return await self.exchange.fetch_open_orders()

    async def get_positions(self) -> list[dict[str, Any]]:
        """Fetch all positions (futures only)."""
        try:
            return await self.exchange.fetch_positions()
        except Exception:
            return []

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        return await self.exchange.fetch_ticker(symbol)

    @exchange_retry(max_attempts=3)
    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        result = await self.exchange.cancel_order(order_id, symbol)
        log.info("order.cancelled", order_id=order_id, symbol=symbol)
        return result

    async def cancel_all_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Emergency: cancel all open orders."""
        try:
            if symbol:
                orders = await self.exchange.fetch_open_orders(symbol)
            else:
                orders = await self.exchange.fetch_open_orders()
            results = []
            for order in orders:
                try:
                    r = await self.exchange.cancel_order(order["id"], order["symbol"])
                    results.append(r)
                except Exception:
                    log.exception("cancel_all.single_fail", order_id=order["id"])
            log.info("orders.cancelled_all", count=len(results), symbol=symbol)
            return results
        except Exception:
            log.exception("cancel_all.failed")
            return []

    # -- helpers --

    def _round_amount(self, amount: float, market: dict[str, Any]) -> float:
        precision = market.get("precision", {}).get("amount")
        if precision is not None:
            from src.utils.math_utils import round_to_precision

            return round_to_precision(amount, int(precision))
        return amount

    def _round_price(self, price: float, market: dict[str, Any]) -> float:
        precision = market.get("precision", {}).get("price")
        if precision is not None:
            from src.utils.math_utils import round_to_precision

            return round_to_precision(price, int(precision))
        return price

    def get_market_info(self, symbol: str) -> dict[str, Any]:
        return self.exchange.market(symbol)
