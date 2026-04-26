"""Strategy lifecycle manager with regime-based rotation."""

from __future__ import annotations

from typing import Any

import pandas as pd
import structlog

from src.core.event_bus import EventBus, EventType, make_event
from src.indicators.regime import MarketRegime, STRATEGY_MAP
from src.risk.risk_engine import Signal
from src.strategies.base import BaseStrategy
from src.strategies.dca_fear import FearWeightedDCA
from src.strategies.grid_trading import GridTradingStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.momentum import MomentumStrategy

log = structlog.get_logger(__name__)

_STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "grid_trading": GridTradingStrategy,
    "momentum": MomentumStrategy,
    "dca_fear": FearWeightedDCA,
    "mean_reversion": MeanReversionStrategy,
}


class StrategyManager:
    """Manages strategy lifecycle and regime-based rotation."""

    def __init__(
        self,
        strategy_configs: dict[str, dict[str, Any]],
        event_bus: EventBus | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._strategies: dict[str, BaseStrategy] = {}
        self._current_regime = MarketRegime.RANGING

        # Instantiate all strategies
        for name, cfg in strategy_configs.items():
            cls = _STRATEGY_CLASSES.get(name)
            if cls:
                self._strategies[name] = cls(cfg, name)
                log.info("strategy.loaded", name=name)
            else:
                log.warning("strategy.unknown", name=name)

    @property
    def strategies(self) -> dict[str, BaseStrategy]:
        return self._strategies

    def get_active_strategies(self) -> list[BaseStrategy]:
        return [s for s in self._strategies.values() if s.is_active]

    async def on_regime_change(
        self, old_regime: MarketRegime, new_regime: MarketRegime
    ) -> None:
        """Activate/deactivate strategies based on new regime."""
        self._current_regime = new_regime
        should_be_active = set(STRATEGY_MAP.get(new_regime, []))

        for name, strategy in self._strategies.items():
            if name in should_be_active and not strategy.is_active:
                strategy.is_active = True
                log.info("strategy.activated", name=name, regime=new_regime)
            elif name not in should_be_active and strategy.is_active:
                strategy.is_active = False
                log.info("strategy.deactivated", name=name, regime=new_regime)

        if self._event_bus:
            await self._event_bus.publish(
                make_event(
                    EventType.REGIME_CHANGE,
                    "strategy_manager",
                    {
                        "old": old_regime,
                        "new": new_regime,
                        "active": list(should_be_active),
                    },
                )
            )

    async def evaluate_all(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> list[Signal]:
        """Evaluate all active strategies and collect signals."""
        signals: list[Signal] = []
        for strategy in self.get_active_strategies():
            try:
                signal = await strategy.on_candle(symbol, candle, indicators)
                if signal is not None:
                    signals.append(signal)
            except Exception:
                log.exception("strategy.error", name=strategy.strategy_id)
        return signals

    def get_all_states(self) -> dict[str, dict[str, Any]]:
        return {name: s.get_state() for name, s in self._strategies.items()}

    def load_all_states(self, states: dict[str, dict[str, Any]]) -> None:
        for name, state in states.items():
            if name in self._strategies:
                self._strategies[name].load_state(state)

    def activate_for_regime(self, regime: MarketRegime) -> None:
        """Synchronous activation — used at startup."""
        should_be_active = set(STRATEGY_MAP.get(regime, []))
        for name, strategy in self._strategies.items():
            strategy.is_active = name in should_be_active
