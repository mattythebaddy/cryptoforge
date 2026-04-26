"""Walk-forward optimization to prevent overfitting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
import structlog

from src.backtesting.engine import BacktestEngine, BacktestResult
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward results."""

    windows: list[dict[str, Any]] = field(default_factory=list)
    aggregate_sharpe: float = 0.0
    aggregate_profit_factor: float = 0.0
    aggregate_win_rate: float = 0.0
    aggregate_max_drawdown: float = 0.0
    parameter_stability: float = 0.0  # 0-1, higher = more stable
    passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)


class WalkForwardOptimizer:
    """
    Rolling-window optimization — the ONLY acceptable method.
    In-sample: optimize. Out-of-sample: validate. No cherry-picking.
    """

    def __init__(
        self,
        is_months: int = 12,
        oos_months: int = 6,
        step_months: int = 3,
        maker_fee_pct: float = 0.1,
        taker_fee_pct: float = 0.1,
        slippage_pct: float = 0.05,
    ) -> None:
        self._is_months = is_months
        self._oos_months = oos_months
        self._step_months = step_months
        self._engine = BacktestEngine(maker_fee_pct, taker_fee_pct, slippage_pct)

    async def run(
        self,
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        df: pd.DataFrame,
        param_grid: list[dict[str, Any]],
        initial_capital: float = 10000.0,
    ) -> WalkForwardResult:
        """
        Run walk-forward optimization.

        strategy_factory: callable that takes a config dict and returns a BaseStrategy
        param_grid: list of parameter combinations to test
        """
        result = WalkForwardResult()

        # Generate windows
        windows = self._generate_windows(df)
        if len(windows) < 4:
            result.rejection_reasons.append(f"Only {len(windows)} windows (need >=4)")
            return result

        oos_results: list[BacktestResult] = []

        for w_idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows):
            is_data = df.iloc[is_start:is_end]
            oos_data = df.iloc[oos_start:oos_end]

            # Optimize on IS data
            best_params = None
            best_sharpe = -float("inf")

            for params in param_grid:
                strategy = strategy_factory(params)
                bt = await self._engine.run(strategy, is_data, initial_capital)
                if bt.sharpe_ratio > best_sharpe:
                    best_sharpe = bt.sharpe_ratio
                    best_params = params

            if best_params is None:
                continue

            # Validate on OOS data
            strategy = strategy_factory(best_params)
            oos_bt = await self._engine.run(strategy, oos_data, initial_capital)
            oos_results.append(oos_bt)

            result.windows.append({
                "window": w_idx,
                "is_range": f"{is_start}-{is_end}",
                "oos_range": f"{oos_start}-{oos_end}",
                "best_params": best_params,
                "is_sharpe": best_sharpe,
                "oos_sharpe": oos_bt.sharpe_ratio,
                "oos_return": oos_bt.total_return_pct,
                "oos_drawdown": oos_bt.max_drawdown_pct,
            })

        if not oos_results:
            result.rejection_reasons.append("No valid OOS results")
            return result

        # Aggregate OOS metrics
        result.aggregate_sharpe = sum(r.sharpe_ratio for r in oos_results) / len(oos_results)
        pfs = [r.profit_factor for r in oos_results if r.profit_factor < float("inf")]
        result.aggregate_profit_factor = sum(pfs) / len(pfs) if pfs else 0
        result.aggregate_win_rate = sum(r.win_rate for r in oos_results) / len(oos_results)
        result.aggregate_max_drawdown = max(r.max_drawdown_pct for r in oos_results)

        # Acceptance criteria
        if result.aggregate_sharpe < 0.5:
            result.rejection_reasons.append(f"OOS Sharpe {result.aggregate_sharpe:.2f} < 0.5")
        if result.aggregate_profit_factor < 1.5:
            result.rejection_reasons.append(f"OOS PF {result.aggregate_profit_factor:.2f} < 1.5")
        if result.aggregate_win_rate < 40:
            result.rejection_reasons.append(f"OOS Win Rate {result.aggregate_win_rate:.1f}% < 40%")
        if result.aggregate_max_drawdown > 25:
            result.rejection_reasons.append(f"OOS Max DD {result.aggregate_max_drawdown:.1f}% > 25%")

        result.passed = len(result.rejection_reasons) == 0
        return result

    def _generate_windows(self, df: pd.DataFrame) -> list[tuple[int, int, int, int]]:
        """Generate (is_start, is_end, oos_start, oos_end) index tuples."""
        n = len(df)
        # Rough: assume 1 month ≈ 720 hourly candles
        rows_per_month = max(1, n // max(1, 24))  # adaptive
        # Use fixed estimate: 30 days * 24 hours
        rows_per_month = 720

        is_len = self._is_months * rows_per_month
        oos_len = self._oos_months * rows_per_month
        step = self._step_months * rows_per_month
        total_window = is_len + oos_len

        windows = []
        start = 0
        while start + total_window <= n:
            is_start = start
            is_end = start + is_len
            oos_start = is_end
            oos_end = is_end + oos_len
            windows.append((is_start, is_end, oos_start, oos_end))
            start += step

        return windows
