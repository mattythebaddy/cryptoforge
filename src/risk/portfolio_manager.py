"""Cross-strategy exposure and correlation management."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# Known high-correlation pairs
_CORRELATION_MAP: dict[tuple[str, str], float] = {
    ("BTC/USDT", "ETH/USDT"): 0.75,
    ("ETH/USDT", "BTC/USDT"): 0.75,
    ("BTC/USDT", "SOL/USDT"): 0.65,
    ("SOL/USDT", "BTC/USDT"): 0.65,
    ("ETH/USDT", "SOL/USDT"): 0.70,
    ("SOL/USDT", "ETH/USDT"): 0.70,
}


class Position:
    def __init__(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        strategy_id: str,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.size = size
        self.entry_price = entry_price
        self.strategy_id = strategy_id

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


class PortfolioManager:
    """Tracks open positions across all strategies."""

    def __init__(self, max_exposure_pct: float = 80.0) -> None:
        self._max_exposure_pct = max_exposure_pct
        self._positions: list[Position] = []

    def add_position(self, pos: Position) -> None:
        self._positions.append(pos)

    def remove_position(self, symbol: str, strategy_id: str) -> None:
        self._positions = [
            p
            for p in self._positions
            if not (p.symbol == symbol and p.strategy_id == strategy_id)
        ]

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        if symbol:
            return [p for p in self._positions if p.symbol == symbol]
        return list(self._positions)

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def total_exposure(self) -> float:
        """Total notional value of all open positions."""
        return sum(p.notional for p in self._positions)

    def exposure_pct(self, equity: float) -> float:
        if equity <= 0:
            return 100.0
        return (self.total_exposure() / equity) * 100

    def would_exceed_exposure(self, additional_notional: float, equity: float) -> bool:
        current = self.total_exposure()
        if equity <= 0:
            return True
        new_pct = ((current + additional_notional) / equity) * 100
        return new_pct > self._max_exposure_pct

    def has_position(self, symbol: str, strategy_id: str) -> bool:
        return any(
            p.symbol == symbol and p.strategy_id == strategy_id for p in self._positions
        )

    def correlation_haircut(self, symbol: str, side: str) -> float:
        """
        Returns multiplier (0-1) to reduce size based on correlated positions.
        If already long BTC and want to go long ETH, reduce size.
        """
        max_corr = 0.0
        for pos in self._positions:
            if pos.side == side:
                corr = _CORRELATION_MAP.get((pos.symbol, symbol), 0.0)
                max_corr = max(max_corr, corr)

        if max_corr > 0:
            haircut = 1.0 - max_corr
            log.debug(
                "portfolio.correlation_haircut",
                symbol=symbol,
                correlated_with=[p.symbol for p in self._positions if p.side == side],
                haircut=round(haircut, 2),
            )
            return haircut
        return 1.0
