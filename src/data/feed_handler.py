"""WebSocket + REST data ingestion via CCXT Pro with REST polling fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from src.core.event_bus import EventBus, EventType, make_event
from src.data.candle_builder import CandleBuilder
from src.utils.time_utils import TIMEFRAME_SECONDS

log = structlog.get_logger(__name__)

# Reconnection parameters
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 30.0
_JITTER_FACTOR = 0.25
_STALE_TIMEOUT = 60.0
_PING_INTERVAL = 30.0


class FeedHandler:
    """
    Maintains WebSocket connections + REST polling fallback.
    REST polling ensures candles arrive even when WebSocket drops.
    """

    def __init__(
        self,
        exchange: Any,
        event_bus: EventBus,
        redis: Any,
        symbols: list[str],
        timeframes: list[str],
    ) -> None:
        self._exchange = exchange
        self._event_bus = event_bus
        self._redis = redis
        self._symbols = symbols
        self._timeframes = timeframes
        self._candle_builder = CandleBuilder(timeframes)
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._last_message_time: dict[str, float] = {}
        # Track last emitted candle timestamp per symbol/tf to avoid duplicates
        self._last_candle_ts: dict[str, int] = {}

    async def start(self) -> None:
        """Start WebSocket feeds AND REST polling."""
        self._running = True

        # WebSocket streams (best-effort, may drop)
        for symbol in self._symbols:
            self._tasks.append(
                asyncio.create_task(
                    self._watch_trades_loop(symbol), name=f"ws_trades_{symbol}"
                )
            )
            self._tasks.append(
                asyncio.create_task(
                    self._watch_orderbook_loop(symbol), name=f"ws_book_{symbol}"
                )
            )

        # REST candle pollers — the RELIABLE fallback
        for symbol in self._symbols:
            for tf in self._timeframes:
                self._tasks.append(
                    asyncio.create_task(
                        self._poll_candles_loop(symbol, tf),
                        name=f"poll_{symbol}_{tf}",
                    )
                )

        self._tasks.append(
            asyncio.create_task(self._staleness_monitor(), name="staleness")
        )

        log.info(
            "feed_handler.started",
            symbols=self._symbols,
            timeframes=self._timeframes,
            mode="websocket+rest_polling",
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("feed_handler.stopped")

    # ----------------------------------------------------------------
    # REST candle polling — guaranteed candle delivery
    # ----------------------------------------------------------------

    async def _poll_candles_loop(self, symbol: str, timeframe: str) -> None:
        """
        Polls exchange REST API for the latest closed candles.
        Runs every (timeframe_seconds / 2) to catch candle closes promptly.
        Deduplicates against already-emitted candles.
        """
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 300)
        poll_interval = max(15, tf_seconds // 2)  # poll at half the candle period, min 15s
        cache_key = f"{symbol}:{timeframe}"

        log.info("poll.started", symbol=symbol, timeframe=timeframe, interval_s=poll_interval)

        while self._running:
            try:
                # Fetch last 3 candles (current forming + 2 closed)
                candles = await self._exchange.fetch_ohlcv(
                    symbol, timeframe, limit=3
                )

                if not candles:
                    await asyncio.sleep(poll_interval)
                    continue

                now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                last_emitted = self._last_candle_ts.get(cache_key, 0)

                for c in candles:
                    candle_ts = int(c[0])
                    candle_end_ts = candle_ts + tf_seconds * 1000

                    # Only emit if: candle is closed AND we haven't emitted it yet
                    if candle_end_ts <= now_ts and candle_ts > last_emitted:
                        candle_time = datetime.fromtimestamp(
                            candle_ts / 1000, tz=timezone.utc
                        ).isoformat()

                        await self._event_bus.publish(
                            make_event(
                                EventType.CANDLE_CLOSED,
                                "rest_poller",
                                {
                                    "symbol": symbol,
                                    "timeframe": timeframe,
                                    "open": float(c[1]),
                                    "high": float(c[2]),
                                    "low": float(c[3]),
                                    "close": float(c[4]),
                                    "volume": float(c[5]) if c[5] else 0.0,
                                    "time": candle_time,
                                },
                            )
                        )
                        self._last_candle_ts[cache_key] = candle_ts
                        self._last_message_time[f"{symbol}_poll"] = asyncio.get_event_loop().time()

                        log.info(
                            "poll.candle_closed",
                            symbol=symbol,
                            timeframe=timeframe,
                            close=float(c[4]),
                            time=candle_time,
                        )

                # Also update price cache from latest candle
                if candles and self._redis:
                    latest_price = float(candles[-1][4])
                    await self._redis.set(
                        f"cryptoforge:price:{symbol}", str(latest_price)
                    )

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("poll.error", symbol=symbol, timeframe=timeframe)

            await asyncio.sleep(poll_interval)

    # ----------------------------------------------------------------
    # WebSocket trade stream (best-effort, builds micro-candles)
    # ----------------------------------------------------------------

    async def _watch_trades_loop(self, symbol: str) -> None:
        backoff = _INITIAL_BACKOFF
        while self._running:
            try:
                trades = await self._exchange.watch_trades(symbol)
                backoff = _INITIAL_BACKOFF
                self._last_message_time[symbol] = asyncio.get_event_loop().time()
                for trade in trades:
                    await self._on_trade(symbol, trade)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("feed_handler.ws_error", symbol=symbol)
                await self._backoff_sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _on_trade(self, symbol: str, trade: dict[str, Any]) -> None:
        price = float(trade.get("price", 0))
        amount = float(trade.get("amount", 0))
        timestamp = int(trade.get("timestamp", 0))
        if price <= 0 or amount <= 0:
            return

        # Update Redis price cache (WebSocket is fastest)
        if self._redis:
            await self._redis.set(f"cryptoforge:price:{symbol}", str(price))

        # Note: we no longer emit CANDLE_CLOSED from WebSocket trades.
        # REST polling is the authoritative candle source — avoids duplicates.

    # ----------------------------------------------------------------
    # WebSocket order book stream
    # ----------------------------------------------------------------

    async def _watch_orderbook_loop(self, symbol: str) -> None:
        backoff = _INITIAL_BACKOFF
        while self._running:
            try:
                book = await self._exchange.watch_order_book(symbol, limit=20)
                backoff = _INITIAL_BACKOFF
                self._last_message_time[f"{symbol}_book"] = asyncio.get_event_loop().time()
                await self._event_bus.publish(
                    make_event(
                        EventType.ORDERBOOK_UPDATE,
                        "feed_handler",
                        {
                            "symbol": symbol,
                            "bids": book.get("bids", [])[:10],
                            "asks": book.get("asks", [])[:10],
                            "timestamp": book.get("timestamp"),
                        },
                    )
                )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("feed_handler.ws_book_error", symbol=symbol)
                await self._backoff_sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    # ----------------------------------------------------------------
    # Staleness monitor
    # ----------------------------------------------------------------

    async def _staleness_monitor(self) -> None:
        while self._running:
            await asyncio.sleep(_PING_INTERVAL)
            now = asyncio.get_event_loop().time()
            for key, last in list(self._last_message_time.items()):
                if now - last > _STALE_TIMEOUT:
                    log.warning("feed_handler.stale", stream=key, gap_s=round(now - last))

    @staticmethod
    async def _backoff_sleep(base: float) -> None:
        import random

        jitter = base * _JITTER_FACTOR * (2 * random.random() - 1)
        await asyncio.sleep(max(0.1, base + jitter))
