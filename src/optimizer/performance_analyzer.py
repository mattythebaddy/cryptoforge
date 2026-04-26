"""Performance analyzer — crunches journal data into actionable metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.optimizer.trade_journal import JournalEntry, TradeJournal

log = structlog.get_logger(__name__)


@dataclass
class StrategyMetrics:
    strategy_id: str
    trade_count: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_losses: int = 0
    avg_hold_duration: float = 0.0
    sharpe_ratio: float = 0.0
    recent_sharpe: float = 0.0  # last 10 trades
    best_regime: str = "unknown"
    worst_regime: str = "unknown"


@dataclass
class IndicatorCorrelation:
    indicator_name: str
    win_median: float
    loss_median: float
    direction: str  # "higher_wins" or "lower_wins"
    score: float  # abs difference normalized


class PerformanceAnalyzer:
    """Analyzes trade journal to produce strategy metrics and correlations."""

    def __init__(self, journal: TradeJournal) -> None:
        self._journal = journal

    def compute_strategy_metrics(
        self, strategy_id: str, last_n: int = 50
    ) -> StrategyMetrics:
        trades = self._journal.get_by_strategy(strategy_id, last_n)
        if not trades:
            return StrategyMetrics(strategy_id=strategy_id)

        wins = [t for t in trades if t.result == "win"]
        losses = [t for t in trades if t.result == "loss"]

        win_rate = len(wins) / len(trades) if trades else 0
        avg_pnl = sum(t.pnl for t in trades) / len(trades)
        total_pnl = sum(t.pnl for t in trades)

        gross_wins = sum(t.pnl for t in wins) if wins else 0
        gross_losses = abs(sum(t.pnl for t in losses)) if losses else 0
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

        # Max consecutive losses
        max_consec = 0
        current_consec = 0
        for t in trades:
            if t.result == "loss":
                current_consec += 1
                max_consec = max(max_consec, current_consec)
            else:
                current_consec = 0

        avg_hold = (
            sum(t.hold_duration_candles for t in trades) / len(trades)
            if trades
            else 0
        )

        sharpe = self._sharpe(trades)
        recent_sharpe = self._sharpe(trades[-10:]) if len(trades) >= 5 else 0

        # Best/worst regime
        regime_pnl: dict[str, list[float]] = {}
        for t in trades:
            regime_pnl.setdefault(t.entry_regime, []).append(t.pnl)

        best_regime = "unknown"
        worst_regime = "unknown"
        if regime_pnl:
            by_avg = {r: sum(pnls) / len(pnls) for r, pnls in regime_pnl.items()}
            best_regime = max(by_avg, key=by_avg.get)  # type: ignore[arg-type]
            worst_regime = min(by_avg, key=by_avg.get)  # type: ignore[arg-type]

        return StrategyMetrics(
            strategy_id=strategy_id,
            trade_count=len(trades),
            win_rate=win_rate,
            avg_pnl=avg_pnl,
            total_pnl=total_pnl,
            profit_factor=profit_factor,
            max_consecutive_losses=max_consec,
            avg_hold_duration=avg_hold,
            sharpe_ratio=sharpe,
            recent_sharpe=recent_sharpe,
            best_regime=best_regime,
            worst_regime=worst_regime,
        )

    def compute_all_metrics(self, last_n: int = 50) -> dict[str, StrategyMetrics]:
        strategy_ids = set(t.strategy_id for t in self._journal.get_recent(500))
        return {sid: self.compute_strategy_metrics(sid, last_n) for sid in strategy_ids}

    def compute_indicator_correlations(
        self, strategy_id: str, last_n: int = 100
    ) -> list[IndicatorCorrelation]:
        trades = self._journal.get_by_strategy(strategy_id, last_n)
        if len(trades) < 10:
            return []

        wins = [t for t in trades if t.result == "win"]
        losses = [t for t in trades if t.result == "loss"]
        if not wins or not losses:
            return []

        # Collect all indicator names
        all_indicators: set[str] = set()
        for t in trades:
            all_indicators.update(t.entry_indicators.keys())

        correlations: list[IndicatorCorrelation] = []
        for ind in all_indicators:
            win_vals = [t.entry_indicators[ind] for t in wins if ind in t.entry_indicators]
            loss_vals = [t.entry_indicators[ind] for t in losses if ind in t.entry_indicators]

            if len(win_vals) < 3 or len(loss_vals) < 3:
                continue

            win_med = sorted(win_vals)[len(win_vals) // 2]
            loss_med = sorted(loss_vals)[len(loss_vals) // 2]

            diff = win_med - loss_med
            value_range = max(max(win_vals + loss_vals) - min(win_vals + loss_vals), 1e-10)
            score = abs(diff) / value_range

            if score < 0.05:
                continue  # not meaningful

            correlations.append(
                IndicatorCorrelation(
                    indicator_name=ind,
                    win_median=win_med,
                    loss_median=loss_med,
                    direction="higher_wins" if diff > 0 else "lower_wins",
                    score=round(score, 3),
                )
            )

        correlations.sort(key=lambda c: c.score, reverse=True)
        return correlations[:5]

    def format_summary(self, metrics: dict[str, StrategyMetrics]) -> str:
        lines = []
        for sid, m in sorted(metrics.items()):
            if m.trade_count == 0:
                continue
            pf_str = f"{m.profit_factor:.1f}" if m.profit_factor < 100 else "INF"
            lines.append(
                f"  {sid}: {m.trade_count} trades, "
                f"{m.win_rate*100:.0f}% WR, PF={pf_str}, "
                f"Sharpe={m.sharpe_ratio:.2f}"
            )
        return "\n".join(lines) if lines else "  No trades yet"

    @staticmethod
    def _sharpe(trades: list[JournalEntry]) -> float:
        if len(trades) < 2:
            return 0.0
        returns = [t.pnl_pct for t in trades]
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 0
        return (mean_r / std) if std > 0 else 0.0
