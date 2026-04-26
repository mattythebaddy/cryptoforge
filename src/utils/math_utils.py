"""Financial math helpers."""

from __future__ import annotations

import math


def round_to_precision(value: float, precision: int) -> float:
    """Round a float to *precision* decimal places (exchange amount/price precision)."""
    if precision <= 0:
        return float(round(value))
    factor = 10**precision
    return math.floor(value * factor) / factor


def pct_change(old: float, new: float) -> float:
    """Percentage change from *old* to *new*."""
    if old == 0:
        return 0.0
    return ((new - old) / abs(old)) * 100


def round_trip_cost_pct(maker_fee: float, taker_fee: float, slippage: float) -> float:
    """Total cost of entering and exiting a position (percentages)."""
    return maker_fee + taker_fee + (2 * slippage)


def half_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Half-Kelly fraction: 0.5 * [W - (1-W)/R].
    Returns fraction of equity to risk (0.0 - 1.0), capped at 0.05.
    """
    if avg_loss == 0 or win_rate <= 0:
        return 0.0
    r = avg_win / avg_loss
    full = win_rate - (1 - win_rate) / r
    return max(0.0, min(full * 0.5, 0.05))


def fixed_fractional_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss_price: float,
) -> float:
    """
    Position size using fixed-fractional method.
    size = (equity * risk_pct) / |entry - stop_loss|
    """
    risk_per_unit = abs(entry_price - stop_loss_price)
    if risk_per_unit == 0:
        return 0.0
    risk_amount = equity * (risk_pct / 100)
    return risk_amount / risk_per_unit


def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio from a list of periodic returns."""
    if len(returns) < 2:
        return 0.0
    import statistics

    mean = statistics.mean(returns) - risk_free_rate
    std = statistics.stdev(returns)
    if std == 0:
        return 0.0
    # assume hourly returns → ~8760 periods per year
    return (mean / std) * math.sqrt(8760)


def max_drawdown(equity_curve: list[float]) -> float:
    """Maximum drawdown percentage from an equity curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd
