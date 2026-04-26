"""Parameter optimizer — nudges strategy params toward winning configurations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.optimizer.performance_analyzer import PerformanceAnalyzer
from src.optimizer.trade_journal import JournalEntry, TradeJournal
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


@dataclass
class ParamBounds:
    min_val: float
    max_val: float
    step: float | None = None  # None = continuous, 1 = integer


@dataclass
class ParamAdjustment:
    strategy_id: str
    param_name: str
    old_value: float
    new_value: float
    reason: str


# Registry of tunable parameters and their safe bounds
TUNABLE_PARAMS: dict[str, dict[str, ParamBounds]] = {
    "momentum": {
        "min_indicators_aligned": ParamBounds(2, 5, 1),
        "atr_stop_multiplier": ParamBounds(1.5, 4.0),
        "trailing_stop_atr_mult": ParamBounds(2.0, 5.0),
        "cooldown_candles": ParamBounds(2, 10, 1),
        "take_profit_rr_ratio": ParamBounds(1.5, 5.0),
        "min_hold_candles": ParamBounds(3, 15, 1),
    },
    "grid_trading": {
        "stop_loss_pct": ParamBounds(2.0, 10.0),
    },
    "mean_reversion": {
        "entry_bb_pct": ParamBounds(0.05, 0.25),
        "max_hold_candles": ParamBounds(10, 50, 1),
        "exit_bb_pct": ParamBounds(0.35, 0.65),
    },
}

# Maps config key -> private attribute name for live updates
_ATTR_MAP: dict[str, dict[str, str]] = {
    "momentum": {
        "min_indicators_aligned": "_min_aligned",
        "atr_stop_multiplier": "_atr_stop_mult",
        "trailing_stop_atr_mult": "_trailing_atr_mult",
        "cooldown_candles": "_cooldown_candles",
        "take_profit_rr_ratio": "_rr_ratio",
        "min_hold_candles": "_min_hold_candles",
    },
    "grid_trading": {
        "stop_loss_pct": "_stop_loss_pct",
    },
    "mean_reversion": {
        "entry_bb_pct": "_entry_bb_pct",
        "max_hold_candles": "_max_hold",
        "exit_bb_pct": "_exit_bb_pct",
    },
}


class ParameterOptimizer:
    """Adjusts strategy parameters based on winning vs losing trade analysis."""

    MAX_ADJUST_PCT = 0.10  # Max 10% change per cycle
    MIN_TRADES = 5  # Need at least this many trades per strategy
    MIN_BUCKET_SIZE = 3  # Need at least this many winners AND losers
    MIN_SIGNIFICANCE_PCT = 0.10  # 10% of param range minimum difference
    STABILITY_TRADES = 5  # Don't re-nudge a param within this many trades

    def __init__(
        self, analyzer: PerformanceAnalyzer, journal: TradeJournal
    ) -> None:
        self._analyzer = analyzer
        self._journal = journal
        # Track when each param was last nudged (trade count at nudge time)
        self._last_nudge: dict[str, int] = {}  # "strategy.param" -> trade_count

    def optimize(
        self, strategy_id: str, current_config: dict[str, Any]
    ) -> list[ParamAdjustment]:
        """Compute parameter adjustments. Does NOT apply them."""
        tunable = TUNABLE_PARAMS.get(strategy_id, {})
        if not tunable:
            return []

        trades = self._journal.get_by_strategy(strategy_id, 100)
        if len(trades) < self.MIN_TRADES:
            return []

        winners = [t for t in trades if t.result == "win"]
        losers = [t for t in trades if t.result == "loss"]
        if not winners or not losers:
            return []

        total_trades = self._journal.total_count
        adjustments: list[ParamAdjustment] = []

        for param_name, bounds in tunable.items():
            current_val = float(current_config.get(param_name, 0))
            if current_val == 0:
                continue

            # Stability check: don't re-nudge too soon
            nudge_key = f"{strategy_id}.{param_name}"
            last = self._last_nudge.get(nudge_key, 0)
            if total_trades - last < self.STABILITY_TRADES:
                continue

            # Get param value from trade snapshots
            win_vals = self._extract_param(winners, param_name)
            loss_vals = self._extract_param(losers, param_name)

            # Require minimum sample in BOTH buckets
            if len(win_vals) < self.MIN_BUCKET_SIZE or len(loss_vals) < self.MIN_BUCKET_SIZE:
                continue

            win_avg = sum(win_vals) / len(win_vals)
            loss_avg = sum(loss_vals) / len(loss_vals)

            # Significance filter: need 10%+ of param range difference
            param_range = bounds.max_val - bounds.min_val
            if abs(win_avg - loss_avg) < self.MIN_SIGNIFICANCE_PCT * param_range:
                continue

            # Nudge toward winner average
            target = win_avg
            max_step = current_val * self.MAX_ADJUST_PCT
            delta = target - current_val
            delta = max(-max_step, min(max_step, delta))  # clamp step size

            new_val = current_val + delta
            new_val = self._clamp(new_val, bounds)

            if abs(new_val - current_val) < 1e-6:
                continue

            direction = "up" if new_val > current_val else "down"
            adjustments.append(
                ParamAdjustment(
                    strategy_id=strategy_id,
                    param_name=param_name,
                    old_value=current_val,
                    new_value=new_val,
                    reason=f"Winners avg={win_avg:.3f} vs losers={loss_avg:.3f} -> nudge {direction}",
                )
            )

        return adjustments

    def apply_adjustments(
        self, strategy: BaseStrategy, adjustments: list[ParamAdjustment]
    ) -> None:
        """Apply adjustments to strategy config dict AND private attributes."""
        attr_map = _ATTR_MAP.get(strategy.strategy_id, {})
        total_trades = self._journal.total_count

        for adj in adjustments:
            # Update config dict
            strategy.config[adj.param_name] = adj.new_value

            # Update private attribute
            attr_name = attr_map.get(adj.param_name)
            if attr_name and hasattr(strategy, attr_name):
                setattr(strategy, attr_name, adj.new_value)

            # Record nudge time for stability tracking
            nudge_key = f"{adj.strategy_id}.{adj.param_name}"
            self._last_nudge[nudge_key] = total_trades

            log.info(
                "param.adjusted",
                strategy=adj.strategy_id,
                param=adj.param_name,
                old=round(adj.old_value, 4),
                new=round(adj.new_value, 4),
                reason=adj.reason,
            )

    @staticmethod
    def _extract_param(trades: list[JournalEntry], param_name: str) -> list[float]:
        vals = []
        for t in trades:
            if param_name in t.strategy_params:
                try:
                    vals.append(float(t.strategy_params[param_name]))
                except (ValueError, TypeError):
                    pass
        return vals

    @staticmethod
    def _clamp(value: float, bounds: ParamBounds) -> float:
        value = max(bounds.min_val, min(bounds.max_val, value))
        if bounds.step is not None:
            value = round(value / bounds.step) * bounds.step
        return value
