"""Timezone handling and candle time alignment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

# Candle durations in seconds
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_to_dt(ts_ms: int) -> datetime:
    """Convert millisecond timestamp to timezone-aware datetime."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def dt_to_ts(dt: datetime) -> int:
    """Convert datetime to millisecond timestamp."""
    return int(dt.timestamp() * 1000)


def align_to_candle(dt: datetime, timeframe: str) -> datetime:
    """Round *dt* down to the start of the candle period."""
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    ts = int(dt.timestamp())
    aligned = ts - (ts % seconds)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def next_candle_close(dt: datetime, timeframe: str) -> datetime:
    """Return the timestamp when the current candle closes."""
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    ts = int(dt.timestamp())
    aligned = ts - (ts % seconds) + seconds
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def timeframe_to_seconds(timeframe: str) -> int:
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    return seconds
