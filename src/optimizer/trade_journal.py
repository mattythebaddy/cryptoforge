"""Trade journal — stores every completed trade with full market context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class JournalEntry:
    """Complete record of a single trade for learning."""

    trade_id: int
    symbol: str
    strategy_id: str
    side: str  # "buy" (long) or "sell" (short)
    entry_price: float
    exit_price: float
    amount: float
    pnl: float
    pnl_pct: float
    result: str  # "win" or "loss"
    hold_duration_candles: int
    entry_reason: str
    exit_reason: str
    entry_indicators: dict[str, float] = field(default_factory=dict)
    entry_regime: str = "unknown"
    exit_regime: str = "unknown"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


class TradeJournal:
    """Append-only trade journal for the optimizer to learn from."""

    MAX_ENTRIES = 500

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._next_id = 1

    def record(self, entry: JournalEntry) -> None:
        entry.trade_id = self._next_id
        self._next_id += 1
        self._entries.append(entry)
        if len(self._entries) > self.MAX_ENTRIES:
            self._entries = self._entries[-self.MAX_ENTRIES:]
        log.info(
            "journal.recorded",
            trade_id=entry.trade_id,
            symbol=entry.symbol,
            strategy=entry.strategy_id,
            pnl=round(entry.pnl, 4),
            result=entry.result,
        )

    def get_recent(self, n: int = 50) -> list[JournalEntry]:
        return self._entries[-n:]

    def get_by_strategy(self, strategy_id: str, n: int = 50) -> list[JournalEntry]:
        filtered = [e for e in self._entries if e.strategy_id == strategy_id]
        return filtered[-n:]

    def get_by_regime(self, regime: str, n: int = 50) -> list[JournalEntry]:
        filtered = [e for e in self._entries if e.entry_regime == regime]
        return filtered[-n:]

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def to_state(self) -> dict[str, Any]:
        return {
            "entries": [asdict(e) for e in self._entries[-self.MAX_ENTRIES:]],
            "next_id": self._next_id,
        }

    def from_state(self, state: dict[str, Any]) -> None:
        self._next_id = state.get("next_id", 1)
        raw = state.get("entries", [])
        self._entries = []
        for d in raw:
            try:
                self._entries.append(JournalEntry(**d))
            except Exception:
                pass  # skip corrupt entries
        log.info("journal.restored", entries=len(self._entries))
