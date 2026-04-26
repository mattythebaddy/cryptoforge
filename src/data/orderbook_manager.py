"""Local order book maintenance and analysis."""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


class OrderBookManager:
    """Maintains local order book snapshots per symbol."""

    def __init__(self) -> None:
        # symbol → {bids: [[price, amount], ...], asks: [[price, amount], ...]}
        self._books: dict[str, dict[str, list[list[float]]]] = {}

    def update(self, symbol: str, bids: list[list[float]], asks: list[list[float]]) -> None:
        self._books[symbol] = {"bids": bids, "asks": asks}

    def get_book(self, symbol: str) -> dict[str, list[list[float]]] | None:
        return self._books.get(symbol)

    def get_mid_price(self, symbol: str) -> float | None:
        book = self._books.get(symbol)
        if not book or not book["bids"] or not book["asks"]:
            return None
        best_bid = book["bids"][0][0]
        best_ask = book["asks"][0][0]
        return (best_bid + best_ask) / 2

    def get_spread_pct(self, symbol: str) -> float | None:
        book = self._books.get(symbol)
        if not book or not book["bids"] or not book["asks"]:
            return None
        best_bid = book["bids"][0][0]
        best_ask = book["asks"][0][0]
        mid = (best_bid + best_ask) / 2
        if mid == 0:
            return None
        return ((best_ask - best_bid) / mid) * 100

    def estimate_fill_price(
        self, symbol: str, side: str, amount: float
    ) -> float | None:
        """Walk the book to estimate volume-weighted average fill price."""
        book = self._books.get(symbol)
        if not book:
            return None

        levels = book["asks"] if side == "buy" else book["bids"]
        if not levels:
            return None

        remaining = amount
        total_cost = 0.0
        for price, qty in levels:
            fill = min(remaining, qty)
            total_cost += fill * price
            remaining -= fill
            if remaining <= 0:
                break

        filled = amount - remaining
        if filled <= 0:
            return None
        return total_cost / filled
