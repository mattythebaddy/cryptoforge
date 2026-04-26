"""Position sizing methods — Kelly, fixed-fractional, ATR-based."""

from __future__ import annotations

import structlog

from src.utils.math_utils import half_kelly as _half_kelly, fixed_fractional_size

log = structlog.get_logger(__name__)

# Absolute cap: never risk more than 5% of equity on a single trade
_MAX_EQUITY_FRACTION = 0.05


class PositionSizer:
    """Calculate optimal position sizes for trades."""

    def half_kelly(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        equity: float,
    ) -> float:
        """
        Half-Kelly position size in base currency.
        Capped at 5% of equity.
        """
        fraction = _half_kelly(win_rate, avg_win, avg_loss)
        size_usd = equity * fraction
        max_usd = equity * _MAX_EQUITY_FRACTION
        result = min(size_usd, max_usd)
        log.debug(
            "sizer.half_kelly",
            win_rate=win_rate,
            fraction=round(fraction, 4),
            size_usd=round(result, 2),
        )
        return result

    def fixed_fractional(
        self,
        equity: float,
        risk_pct: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> float:
        """
        Fixed-fractional: size = (equity * risk%) / |entry - stop|.
        Returns position size in asset units.
        """
        size = fixed_fractional_size(equity, risk_pct, entry_price, stop_loss_price)
        max_size = (equity * _MAX_EQUITY_FRACTION) / entry_price if entry_price > 0 else 0
        result = min(size, max_size)
        log.debug(
            "sizer.fixed_fractional",
            equity=equity,
            risk_pct=risk_pct,
            size=round(result, 8),
        )
        return result

    def atr_based(
        self,
        equity: float,
        risk_pct: float,
        entry_price: float,
        atr: float,
        atr_multiplier: float = 2.0,
    ) -> float:
        """ATR-based sizing — stop distance = ATR * multiplier."""
        stop_distance = atr * atr_multiplier
        stop_loss = entry_price - stop_distance  # long side
        return self.fixed_fractional(equity, risk_pct, entry_price, stop_loss)

    def calculate(
        self,
        method: str,
        equity: float,
        entry_price: float,
        stop_loss_price: float | None = None,
        risk_pct: float = 1.0,
        win_rate: float = 0.5,
        avg_win: float = 1.5,
        avg_loss: float = 1.0,
        atr: float | None = None,
    ) -> float:
        """
        Unified entry point — dispatches to the configured method.
        Returns position size in asset units.
        """
        if method == "half_kelly":
            size_usd = self.half_kelly(win_rate, avg_win, avg_loss, equity)
            return size_usd / entry_price if entry_price > 0 else 0.0

        if method == "quarter_kelly":
            fraction = _half_kelly(win_rate, avg_win, avg_loss) / 2  # quarter
            size_usd = min(equity * fraction, equity * _MAX_EQUITY_FRACTION)
            return size_usd / entry_price if entry_price > 0 else 0.0

        if method == "fixed_fractional":
            if stop_loss_price is None:
                log.warning("sizer.no_stop_loss", msg="fixed_fractional requires stop_loss")
                return 0.0
            return self.fixed_fractional(equity, risk_pct, entry_price, stop_loss_price)

        log.warning("sizer.unknown_method", method=method)
        return 0.0
