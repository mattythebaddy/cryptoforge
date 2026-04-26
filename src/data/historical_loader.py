"""Historical OHLCV data backfill from exchanges."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from src.data.models import get_session
from src.utils.time_utils import dt_to_ts, timeframe_to_seconds

log = structlog.get_logger(__name__)

# Binance max candles per request
_MAX_PER_REQUEST = 1000
_RATE_LIMIT_SLEEP = 0.5  # seconds between requests


class HistoricalLoader:
    """Downloads and stores historical OHLCV data."""

    def __init__(self, exchange: Any) -> None:
        self._exchange = exchange

    async def backfill(
        self,
        symbol: str,
        timeframe: str,
        since: datetime,
        until: datetime | None = None,
    ) -> int:
        """
        Download candles and store in TimescaleDB.
        Returns count of candles stored.
        """
        until = until or datetime.now(timezone.utc)
        since_ms = dt_to_ts(since)
        until_ms = dt_to_ts(until)
        tf_ms = timeframe_to_seconds(timeframe) * 1000
        total_stored = 0

        log.info(
            "backfill.start",
            symbol=symbol,
            timeframe=timeframe,
            since=since.isoformat(),
            until=until.isoformat(),
        )

        cursor = since_ms
        while cursor < until_ms:
            try:
                candles = await self._exchange.fetch_ohlcv(
                    symbol, timeframe, since=cursor, limit=_MAX_PER_REQUEST
                )
            except Exception:
                log.exception("backfill.fetch_error", symbol=symbol, cursor=cursor)
                await asyncio.sleep(_RATE_LIMIT_SLEEP * 4)
                continue

            if not candles:
                break

            # Store batch
            count = await self._store_candles(symbol, timeframe, candles)
            total_stored += count

            # Advance cursor past the last candle
            last_ts = candles[-1][0]
            cursor = last_ts + tf_ms

            # Rate limit
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

            if len(candles) < _MAX_PER_REQUEST:
                break  # no more data available

        log.info("backfill.complete", symbol=symbol, timeframe=timeframe, candles=total_stored)
        return total_stored

    async def backfill_warmup(
        self, symbols: list[str], timeframes: list[str], candle_count: int = 500
    ) -> None:
        """Backfill last N candles for each symbol/timeframe for indicator warmup."""
        for symbol in symbols:
            for tf in timeframes:
                tf_seconds = timeframe_to_seconds(tf)
                since = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - (candle_count * tf_seconds),
                    tz=timezone.utc,
                )
                await self.backfill(symbol, tf, since)

    async def _store_candles(
        self, symbol: str, timeframe: str, candles: list[list[Any]]
    ) -> int:
        """Upsert candles into TimescaleDB. Returns count stored."""
        if not candles:
            return 0

        values = []
        for c in candles:
            ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
            values.append(
                {
                    "time": ts,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]) if c[5] else 0.0,
                }
            )

        async with get_session() as session:
            for v in values:
                await session.execute(
                    text(
                        "INSERT INTO ohlcv (time, symbol, timeframe, open, high, low, close, volume) "
                        "VALUES (:time, :symbol, :timeframe, :open, :high, :low, :close, :volume) "
                        "ON CONFLICT (time, symbol, timeframe) DO NOTHING"
                    ),
                    v,
                )
            await session.commit()

        return len(values)
