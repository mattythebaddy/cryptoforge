"""Build OHLCV candles from raw trade ticks."""

from __future__ import annotations

from typing import Any

from src.utils.time_utils import TIMEFRAME_SECONDS


class CandleBuilder:
    """
    Builds candles in real-time from trades.
    More accurate than exchange candle streams — no missed candles on reconnect.
    """

    def __init__(self, timeframes: list[str]) -> None:
        self._timeframes = timeframes
        # _current[symbol][timeframe] = {open, high, low, close, volume, start_ts}
        self._current: dict[str, dict[str, dict[str, Any]]] = {}

    def process_trade(
        self, symbol: str, price: float, amount: float, timestamp_ms: int
    ) -> list[dict[str, Any]]:
        """
        Process a trade tick. Returns list of completed candles (if any timeframe closed).
        """
        closed: list[dict[str, Any]] = []

        if symbol not in self._current:
            self._current[symbol] = {}

        for tf in self._timeframes:
            tf_seconds = TIMEFRAME_SECONDS.get(tf)
            if tf_seconds is None:
                continue

            ts_s = timestamp_ms / 1000
            candle_start = int(ts_s) - (int(ts_s) % tf_seconds)
            candle_start_ms = candle_start * 1000

            candle = self._current[symbol].get(tf)

            if candle is None:
                # First trade for this symbol/timeframe — start new candle
                self._current[symbol][tf] = self._new_candle(
                    price, amount, candle_start_ms
                )
                continue

            if candle_start_ms > candle["start_ts"]:
                # New candle period — the old candle is complete
                closed.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "time": self._ms_to_iso(candle["start_ts"]),
                        "open": candle["open"],
                        "high": candle["high"],
                        "low": candle["low"],
                        "close": candle["close"],
                        "volume": candle["volume"],
                    }
                )
                # Start new candle
                self._current[symbol][tf] = self._new_candle(
                    price, amount, candle_start_ms
                )
            else:
                # Same candle — update HLCV
                candle["high"] = max(candle["high"], price)
                candle["low"] = min(candle["low"], price)
                candle["close"] = price
                candle["volume"] += amount

        return closed

    def get_current_candle(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        """Return the currently forming (incomplete) candle."""
        return self._current.get(symbol, {}).get(timeframe)

    @staticmethod
    def _new_candle(price: float, amount: float, start_ts: int) -> dict[str, Any]:
        return {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": amount,
            "start_ts": start_ts,
        }

    @staticmethod
    def _ms_to_iso(ts_ms: int) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
