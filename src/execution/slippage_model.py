"""Realistic slippage estimation for live and backtest."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# Base slippage by market cap tier
_BASE_SLIPPAGE = {
    "BTC/USDT": 0.05,
    "ETH/USDT": 0.05,
}
_MID_CAP_SLIPPAGE = 0.10
_SMALL_CAP_SLIPPAGE = 0.30

# Size tiers
_SIZE_FACTORS = [
    (1000, 1.0),
    (10000, 1.5),
    (float("inf"), 2.0),
]


class SlippageModel:
    """Estimates realistic slippage for backtesting and live pre-trade analysis."""

    def estimate_slippage(
        self,
        symbol: str,
        side: str,
        amount_usd: float,
        order_book: dict | None = None,
        current_atr_pct: float | None = None,
        avg_atr_pct: float | None = None,
    ) -> float:
        """
        Returns estimated slippage as a percentage.
        """
        if order_book:
            return self._from_order_book(side, amount_usd, order_book)
        return self._estimate(symbol, amount_usd, current_atr_pct, avg_atr_pct)

    def _from_order_book(
        self, side: str, amount_usd: float, book: dict
    ) -> float:
        """Walk the book for volume-weighted average fill price."""
        levels = book.get("asks" if side == "buy" else "bids", [])
        if not levels:
            return _MID_CAP_SLIPPAGE

        mid = (levels[0][0] + book.get("bids", levels)[0][0]) / 2 if book.get("bids") else levels[0][0]
        remaining_usd = amount_usd
        total_cost = 0.0

        for price, qty in levels:
            level_usd = price * qty
            fill_usd = min(remaining_usd, level_usd)
            total_cost += fill_usd
            remaining_usd -= fill_usd
            if remaining_usd <= 0:
                break

        filled = amount_usd - remaining_usd
        if filled <= 0:
            return _MID_CAP_SLIPPAGE

        avg_price = total_cost / (filled / levels[0][0]) if levels[0][0] > 0 else 0
        if mid > 0 and avg_price > 0:
            slippage = abs(avg_price - mid) / mid * 100
            return min(slippage, 1.0)
        return _MID_CAP_SLIPPAGE

    def _estimate(
        self,
        symbol: str,
        amount_usd: float,
        current_atr_pct: float | None,
        avg_atr_pct: float | None,
    ) -> float:
        """Heuristic slippage when no order book available."""
        base = _BASE_SLIPPAGE.get(symbol, _MID_CAP_SLIPPAGE)

        # Volatility factor
        vol_factor = 1.0
        if current_atr_pct and avg_atr_pct and avg_atr_pct > 0:
            vol_factor = current_atr_pct / avg_atr_pct

        # Size factor
        size_factor = 1.0
        for threshold, factor in _SIZE_FACTORS:
            if amount_usd <= threshold:
                size_factor = factor
                break

        result = base * vol_factor * size_factor
        return min(result, 1.0)  # cap at 1%
