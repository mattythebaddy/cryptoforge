"""Performance report generation for backtests."""

from __future__ import annotations

from typing import Any

from src.backtesting.engine import BacktestResult
from src.backtesting.monte_carlo import MonteCarloResult


def format_backtest_report(result: BacktestResult, title: str = "Backtest Report") -> str:
    """Generate a human-readable text report."""
    lines = [
        f"{'=' * 60}",
        f"  {title}",
        f"{'=' * 60}",
        "",
        "--- PERFORMANCE ---",
        f"  Total Return:        {result.total_return_pct:+.2f}%",
        f"  Buy & Hold Return:   {result.buy_and_hold_return_pct:+.2f}%",
        f"  Alpha:               {result.alpha:+.2f}%",
        "",
        "--- RISK ---",
        f"  Max Drawdown:        {result.max_drawdown_pct:.2f}%",
        f"  Sharpe Ratio:        {result.sharpe_ratio:.2f}",
        f"  Sortino Ratio:       {result.sortino_ratio:.2f}",
        f"  Calmar Ratio:        {result.calmar_ratio:.2f}",
        "",
        "--- TRADE STATISTICS ---",
        f"  Total Trades:        {result.total_trades}",
        f"  Win Rate:            {result.win_rate:.1f}%",
        f"  Profit Factor:       {result.profit_factor:.2f}",
        f"  Avg Trade P&L:       {result.avg_trade_pnl_pct:+.2f}%",
        f"  Avg Winner:          {result.avg_winner_pct:+.2f}%",
        f"  Avg Loser:           {result.avg_loser_pct:+.2f}%",
        f"  Best Trade:          {result.best_trade_pct:+.2f}%",
        f"  Worst Trade:         {result.worst_trade_pct:+.2f}%",
        f"  Avg Holding Period:  {result.avg_holding_period}",
        f"  Expectancy:          {result.expectancy:+.3f}%",
        "",
        "--- FEES ---",
        f"  Total Fees Paid:     ${result.total_fees_paid:.2f}",
        f"  Fees as % of Profit: {result.fees_as_pct_of_profit:.1f}%",
        f"{'=' * 60}",
    ]
    return "\n".join(lines)


def format_monte_carlo_report(mc: MonteCarloResult, initial: float = 10000.0) -> str:
    """Generate Monte Carlo report."""
    lines = [
        f"{'=' * 60}",
        "  Monte Carlo Simulation Results",
        f"  ({mc.simulations} simulations, ${initial:.0f} initial)",
        f"{'=' * 60}",
        "",
        "--- FINAL EQUITY DISTRIBUTION ---",
        f"   5th pct (worst):  ${mc.p5_equity:,.2f}  ({(mc.p5_equity/initial-1)*100:+.1f}%)",
        f"  25th pct:          ${mc.p25_equity:,.2f}  ({(mc.p25_equity/initial-1)*100:+.1f}%)",
        f"  50th pct (median): ${mc.p50_equity:,.2f}  ({(mc.p50_equity/initial-1)*100:+.1f}%)",
        f"  75th pct:          ${mc.p75_equity:,.2f}  ({(mc.p75_equity/initial-1)*100:+.1f}%)",
        f"  95th pct (best):   ${mc.p95_equity:,.2f}  ({(mc.p95_equity/initial-1)*100:+.1f}%)",
        "",
        "--- MAX DRAWDOWN DISTRIBUTION ---",
        f"   5th pct:  {mc.p5_drawdown:.1f}%",
        f"  50th pct:  {mc.p50_drawdown:.1f}%",
        f"  95th pct:  {mc.p95_drawdown:.1f}%",
        "",
        "--- PROBABILITIES ---",
        f"  P(profit):  {mc.probability_of_profit*100:.1f}%",
        f"  P(ruin):    {mc.probability_of_ruin*100:.1f}%",
        f"{'=' * 60}",
    ]
    return "\n".join(lines)
