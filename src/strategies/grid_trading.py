"""Grid trading strategy — profits from price oscillation in a range."""

from __future__ import annotations

from typing import Any

import pandas as pd
import structlog

from src.risk.risk_engine import Signal
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class GridTradingStrategy(BaseStrategy):
    """
    Places buy/sell orders at regular price intervals.
    Profits from price oscillation within a range.
    """

    def __init__(self, config: dict[str, Any], strategy_id: str = "grid_trading") -> None:
        super().__init__(config, strategy_id)
        self._upper = float(config.get("upper_price", 70000))
        self._lower = float(config.get("lower_price", 60000))
        self._num_grids = int(config.get("num_grids", 20))
        self._total_investment = float(config.get("total_investment", 5000))
        self._stop_loss_pct = float(config.get("stop_loss_pct", 15.0))
        self._grid_type = config.get("grid_type", "arithmetic")

        # Calculate grid levels
        self._levels = self._calculate_levels()
        self._per_grid_amount = self._total_investment / self._num_grids

        # Track grid state: level_index → "empty" | "buy_pending" | "bought"
        self._grid_state: dict[int, str] = {i: "empty" for i in range(self._num_grids)}
        self._round_trips = 0
        self._total_profit = 0.0

    def _calculate_levels(self) -> list[float]:
        if self._grid_type == "geometric":
            import math

            ratio = (self._upper / self._lower) ** (1 / self._num_grids)
            return [self._lower * (ratio**i) for i in range(self._num_grids + 1)]
        else:
            spacing = (self._upper - self._lower) / self._num_grids
            return [self._lower + i * spacing for i in range(self._num_grids + 1)]

    async def on_candle(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> Signal | None:
        if not self.is_active or indicators.empty:
            return None

        # Only trade our configured symbol
        configured_symbol = self.config.get("symbol", "")
        if configured_symbol and symbol != configured_symbol:
            return None

        price = float(candle.get("close", 0))
        if price <= 0:
            return None

        # Check if price is outside grid range
        if price < self._lower * (1 - self._stop_loss_pct / 100):
            log.warning("grid.below_stop", price=price, stop=self._lower * (1 - self._stop_loss_pct / 100))
            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side="sell",
                signal_type="exit",
                price=price,
                reason="Price below grid stop loss",
                order_type="market",
            )

        if price > self._upper or price < self._lower:
            return None  # outside range, wait

        # Find current grid level
        current_level = self._find_level(price)
        if current_level is None:
            log.info("grid.no_level", price=price)
            return None

        # Look for buy opportunities below price
        for i in range(current_level, -1, -1):
            if self._grid_state.get(i) == "empty":
                buy_price = self._levels[i]
                sell_price = self._levels[i + 1] if i + 1 < len(self._levels) else self._upper
                amount = self._per_grid_amount / buy_price

                # State changes on fill only (via on_trade_update), not on signal
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    side="buy",
                    signal_type="entry",
                    price=buy_price,
                    amount=amount,
                    stop_loss=self._lower * (1 - self._stop_loss_pct / 100),
                    take_profit=sell_price,
                    order_type="limit",
                    confidence=0.6,
                    reason=f"Grid buy at level {i} (${buy_price:.2f})",
                    metadata={"grid_level": i},
                )

        return None

    async def on_trade_update(self, trade: dict[str, Any]) -> Signal | None:
        """When a grid buy fills, mark it and prepare the sell."""
        grid_level = trade.get("metadata", {}).get("grid_level")
        if grid_level is not None:
            if trade.get("side") == "buy":
                self._grid_state[grid_level] = "bought"
            elif trade.get("side") == "sell":
                self._grid_state[grid_level] = "empty"
                self._round_trips += 1
                pnl = trade.get("realized_pnl", 0)
                self._total_profit += pnl
        return None

    def _find_level(self, price: float) -> int | None:
        for i in range(len(self._levels) - 1):
            if self._levels[i] <= price < self._levels[i + 1]:
                return i
        return None

    def get_required_indicators(self) -> list[str]:
        return ["atr_14"]

    def get_required_timeframes(self) -> list[str]:
        return [self.config.get("timeframe", "5m")]

    def get_state(self) -> dict[str, Any]:
        base = super().get_state()
        base.update({
            "grid_state": {str(k): v for k, v in self._grid_state.items()},
            "round_trips": self._round_trips,
            "total_profit": self._total_profit,
        })
        return base

    def load_state(self, state: dict[str, Any]) -> None:
        super().load_state(state)
        raw = state.get("grid_state", {})
        self._grid_state = {int(k): v for k, v in raw.items()} if raw else self._grid_state
        self._round_trips = state.get("round_trips", 0)
        self._total_profit = state.get("total_profit", 0.0)
