"""Abstract strategy interface — all strategies inherit from this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from src.risk.risk_engine import Signal


class BaseStrategy(ABC):
    """
    Strategies produce SIGNALS, never orders directly.
    The risk engine decides whether to execute.
    """

    def __init__(self, config: dict[str, Any], strategy_id: str) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.is_active = False
        self._cooldown_until: int = 0  # candle index to wait until

    @abstractmethod
    async def on_candle(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> Signal | None:
        """Called when a new candle closes. Return a Signal or None."""

    async def on_trade_update(self, trade: dict[str, Any]) -> Signal | None:
        """Called when a trade fills. Override if strategy needs to react."""
        return None

    @abstractmethod
    def get_required_indicators(self) -> list[str]:
        """Return list of indicator column names this strategy needs."""

    @abstractmethod
    def get_required_timeframes(self) -> list[str]:
        """Return list of timeframes this strategy needs."""

    def get_state(self) -> dict[str, Any]:
        """Return strategy-specific state for persistence."""
        return {"cooldown_until": self._cooldown_until}

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore strategy state after crash recovery."""
        self._cooldown_until = state.get("cooldown_until", 0)

    def _is_cooling_down(self, candle_index: int) -> bool:
        return candle_index < self._cooldown_until

    def _set_cooldown(self, candle_index: int, candles: int) -> None:
        self._cooldown_until = candle_index + candles
