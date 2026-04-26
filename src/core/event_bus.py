"""Redis pub/sub event bus for inter-module communication."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Coroutine

import orjson
import redis.asyncio as aioredis
import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)

# Max events kept in the replay list
_REPLAY_BUFFER = 1000
_REPLAY_KEY = "cryptoforge:events:replay"


class EventType(StrEnum):
    # Market data
    CANDLE_CLOSED = "candle_closed"
    ORDERBOOK_UPDATE = "orderbook_update"
    TRADE = "trade"
    # Signals
    SIGNAL_ENTRY = "signal_entry"
    SIGNAL_EXIT = "signal_exit"
    # Orders
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_FAILED = "order_failed"
    # Risk
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"
    # System
    REGIME_CHANGE = "regime_change"
    HEALTH_CHECK = "health_check"
    SHUTDOWN = "shutdown"


class Event(BaseModel):
    event_type: EventType
    timestamp: str
    source: str
    data: dict[str, Any] = {}

    def serialize(self) -> bytes:
        return orjson.dumps(self.model_dump())

    @classmethod
    def deserialize(cls, raw: bytes) -> Event:
        return cls.model_validate(orjson.loads(raw))


# Type alias for handler coroutines
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Redis-backed async event bus with pub/sub and replay buffer."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._listener_task: asyncio.Task[None] | None = None
        self._running = False

    async def connect(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=False)
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        log.info("event_bus.connected", redis_url=self._redis_url)

    async def start(self) -> None:
        """Start listening for events in a background task."""
        if not self._pubsub:
            raise RuntimeError("EventBus not connected — call connect() first")
        # subscribe to all event type channels
        channels = [et.value for et in EventType]
        await self._pubsub.subscribe(*channels)
        self._running = True
        self._listener_task = asyncio.create_task(self._listen(), name="event_bus_listener")
        log.info("event_bus.started", channels=len(channels))

    async def stop(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        log.info("event_bus.stopped")

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        payload = event.serialize()

        if self._redis:
            try:
                # publish on the channel
                await self._redis.publish(event.event_type.value, payload)
                # store in replay buffer
                await self._redis.lpush(_REPLAY_KEY, payload)
                await self._redis.ltrim(_REPLAY_KEY, 0, _REPLAY_BUFFER - 1)
                return  # Redis worked — listeners will pick it up
            except Exception:
                pass  # Fall through to local dispatch

        # Local-only fallback: call handlers directly when Redis is unavailable
        await self._dispatch(payload)

    async def replay(self, count: int = 100) -> list[Event]:
        """Return the last *count* events from the replay buffer."""
        if not self._redis:
            return []
        raw_list = await self._redis.lrange(_REPLAY_KEY, 0, count - 1)
        return [Event.deserialize(r) for r in raw_list]

    # -- internals --

    async def _listen(self) -> None:
        assert self._pubsub is not None
        while self._running:
            try:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg and msg["type"] == "message":
                    await self._dispatch(msg["data"])
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("event_bus.listener_error")
                await asyncio.sleep(1)

    async def _dispatch(self, raw: bytes) -> None:
        try:
            event = Event.deserialize(raw)
        except Exception:
            log.exception("event_bus.deserialize_error")
            return
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception(
                    "event_bus.handler_error",
                    event_type=event.event_type,
                    handler=handler.__qualname__,
                )


def make_event(
    event_type: EventType,
    source: str,
    data: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        source=source,
        data=data or {},
    )
