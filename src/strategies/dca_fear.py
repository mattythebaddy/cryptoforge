"""Fear-weighted Dollar Cost Averaging strategy."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import structlog

from src.indicators.sentiment import SentimentAnalyzer
from src.risk.risk_engine import Signal
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class FearWeightedDCA(BaseStrategy):
    """
    DCA with position sizing weighted by Fear & Greed Index.
    Buys more during extreme fear, less during greed.
    """

    def __init__(self, config: dict[str, Any], strategy_id: str = "dca_fear") -> None:
        super().__init__(config, strategy_id)
        self._base_amount = float(config.get("base_buy_amount", 50.0))
        self._min_amount = float(config.get("min_buy_amount", 10.0))
        self._max_amount = float(config.get("max_buy_amount", 500.0))
        self._buy_frequency = config.get("buy_frequency", "daily")
        self._buy_hour_utc = int(config.get("buy_hour_utc", 14))
        self._fng_enabled = config.get("fng_enabled", True)
        self._rsi_filter = config.get("rsi_filter_enabled", True)
        self._price_drop_bonus = config.get("price_drop_bonus", True)

        self._sentiment = SentimentAnalyzer()
        self._last_buy_date: str = ""
        self._total_invested: float = 0
        self._total_bought: float = 0
        self._buy_count: int = 0

    async def on_candle(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> Signal | None:
        if not self.is_active:
            return None

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Check if it's time to buy
        if not self._should_buy(now, today):
            return None

        price = float(candle.get("close", 0))
        if price <= 0:
            return None

        # RSI filter
        if self._rsi_filter and len(indicators) > 14:
            rsi = indicators.iloc[-1].get("rsi_14", 50)
            if not pd.isna(rsi) and rsi > 40:
                log.debug("dca.rsi_filter", rsi=round(rsi, 1), msg="RSI too high, skipping")
                return None

        # Calculate buy amount
        buy_amount = self._base_amount

        if self._fng_enabled:
            fng = await self._sentiment.get_fear_greed_index()
            multiplier = SentimentAnalyzer.get_fear_multiplier(fng["value"])
            buy_amount *= multiplier
            log.info("dca.fng", value=fng["value"], multiplier=multiplier)
            if multiplier == 0:
                return None  # extreme greed, skip

        # Price drop bonus
        if self._price_drop_bonus and len(indicators) >= 24:
            pct_24h = (price - indicators.iloc[-24]["close"]) / indicators.iloc[-24]["close"] * 100
            if pct_24h < -5:
                buy_amount *= 1.5
                log.info("dca.price_drop_bonus", pct_24h=round(pct_24h, 2))

        # Clamp
        buy_amount = max(self._min_amount, min(buy_amount, self._max_amount))
        amount = buy_amount / price

        self._last_buy_date = today

        # Place limit order slightly below price
        limit_price = price * 0.999  # 0.1% below

        return Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side="buy",
            signal_type="entry",
            price=limit_price,
            amount=amount,
            stop_loss=price * 0.92,  # 8% stop for DCA (within 10% risk limit)
            order_type="limit",
            confidence=0.7,
            reason=f"DCA buy ${buy_amount:.2f}",
            metadata={"buy_amount_usd": buy_amount},
        )

    async def on_trade_update(self, trade: dict[str, Any]) -> Signal | None:
        if trade.get("side") == "buy":
            amount_usd = trade.get("metadata", {}).get("buy_amount_usd", 0)
            self._total_invested += amount_usd
            self._total_bought += trade.get("amount", 0)
            self._buy_count += 1
        return None

    def _should_buy(self, now: datetime, today: str) -> bool:
        # Already bought today
        if today == self._last_buy_date:
            return False

        # Check time
        if now.hour != self._buy_hour_utc:
            return False

        # Weekly: only on Mondays
        if self._buy_frequency == "weekly_monday" and now.weekday() != 0:
            return False

        # Biweekly: 1st and 15th
        if self._buy_frequency == "biweekly" and now.day not in (1, 15):
            return False

        return True

    @property
    def average_price(self) -> float:
        if self._total_bought <= 0:
            return 0
        return self._total_invested / self._total_bought

    def get_required_indicators(self) -> list[str]:
        return ["rsi_14"]

    def get_required_timeframes(self) -> list[str]:
        return ["1h"]

    def get_state(self) -> dict[str, Any]:
        base = super().get_state()
        base.update({
            "last_buy_date": self._last_buy_date,
            "total_invested": self._total_invested,
            "total_bought": self._total_bought,
            "buy_count": self._buy_count,
        })
        return base

    def load_state(self, state: dict[str, Any]) -> None:
        super().load_state(state)
        self._last_buy_date = state.get("last_buy_date", "")
        self._total_invested = state.get("total_invested", 0)
        self._total_bought = state.get("total_bought", 0)
        self._buy_count = state.get("buy_count", 0)
