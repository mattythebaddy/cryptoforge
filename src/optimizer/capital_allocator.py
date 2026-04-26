"""Capital allocator — shifts position sizing toward winning strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.optimizer.performance_analyzer import PerformanceAnalyzer, StrategyMetrics

log = structlog.get_logger(__name__)


@dataclass
class AllocationResult:
    strategy_id: str
    old_multiplier: float
    new_multiplier: float
    reason: str


class CapitalAllocator:
    """Dynamically adjusts position size multipliers per strategy."""

    MIN_MULT = 0.25
    MAX_MULT = 2.0

    def __init__(self, analyzer: PerformanceAnalyzer) -> None:
        self._analyzer = analyzer
        self._multipliers: dict[str, float] = {}

    def get_multiplier(self, strategy_id: str) -> float:
        return self._multipliers.get(strategy_id, 1.0)

    def rebalance(
        self, metrics: dict[str, StrategyMetrics]
    ) -> list[AllocationResult]:
        results: list[AllocationResult] = []

        for sid, m in metrics.items():
            if m.trade_count < 5:
                continue

            old_mult = self._multipliers.get(sid, 1.0)
            new_mult = old_mult

            # Emergency cut
            if m.max_consecutive_losses >= 4:
                new_mult = self.MIN_MULT
                reason = f"Emergency: {m.max_consecutive_losses} consecutive losses"

            # Strong performer
            elif m.recent_sharpe > 0.5 and m.win_rate > 0.45:
                new_mult = min(old_mult * 1.10, self.MAX_MULT)
                reason = f"Strong: Sharpe={m.recent_sharpe:.2f}, WR={m.win_rate:.0%}"

            # Steady performer
            elif m.recent_sharpe > 0 and m.win_rate > 0.5:
                new_mult = old_mult  # hold steady
                reason = "Steady"

            # Underperformer
            elif m.recent_sharpe <= 0 or m.win_rate < 0.4:
                new_mult = max(old_mult * 0.85, self.MIN_MULT)
                reason = f"Weak: Sharpe={m.recent_sharpe:.2f}, WR={m.win_rate:.0%}"

            else:
                reason = "No change"

            new_mult = max(self.MIN_MULT, min(self.MAX_MULT, new_mult))
            new_mult = round(new_mult, 2)

            if abs(new_mult - old_mult) > 0.01:
                self._multipliers[sid] = new_mult
                results.append(
                    AllocationResult(
                        strategy_id=sid,
                        old_multiplier=old_mult,
                        new_multiplier=new_mult,
                        reason=reason,
                    )
                )
                log.info(
                    "allocation.changed",
                    strategy=sid,
                    old=old_mult,
                    new=new_mult,
                    reason=reason,
                )

        return results

    def to_state(self) -> dict[str, Any]:
        return {"multipliers": dict(self._multipliers)}

    def from_state(self, state: dict[str, Any]) -> None:
        self._multipliers = state.get("multipliers", {})
        log.info("allocator.restored", multipliers=self._multipliers)
