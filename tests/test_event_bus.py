"""Tests for the Redis event bus."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.event_bus import Event, EventBus, EventType, make_event


class TestEvent:
    def test_serialize_deserialize(self) -> None:
        ev = make_event(EventType.CANDLE_CLOSED, "test", {"symbol": "BTC/USDT"})
        raw = ev.serialize()
        restored = Event.deserialize(raw)
        assert restored.event_type == EventType.CANDLE_CLOSED
        assert restored.source == "test"
        assert restored.data["symbol"] == "BTC/USDT"

    def test_make_event_has_timestamp(self) -> None:
        ev = make_event(EventType.SHUTDOWN, "main")
        assert ev.timestamp  # non-empty ISO string


class TestEventBus:
    @pytest.fixture
    def bus(self, mock_redis: AsyncMock) -> EventBus:
        bus = EventBus("redis://localhost:6379/0")
        bus._redis = mock_redis
        return bus

    @pytest.mark.asyncio
    async def test_publish_stores_in_replay(self, bus: EventBus) -> None:
        ev = make_event(EventType.TRADE, "feed", {"price": 65000})
        await bus.publish(ev)
        bus._redis.publish.assert_called_once()
        bus._redis.lpush.assert_called_once()
        bus._redis.ltrim.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_and_dispatch(self, bus: EventBus) -> None:
        received: list[Event] = []

        async def handler(ev: Event) -> None:
            received.append(ev)

        bus.subscribe(EventType.ORDER_FILLED, handler)

        ev = make_event(EventType.ORDER_FILLED, "exec", {"order_id": "123"})
        await bus._dispatch(ev.serialize())

        assert len(received) == 1
        assert received[0].data["order_id"] == "123"

    @pytest.mark.asyncio
    async def test_dispatch_handles_bad_data(self, bus: EventBus) -> None:
        # Should not raise — logs error internally
        await bus._dispatch(b"not valid json at all")

    @pytest.mark.asyncio
    async def test_replay_returns_events(self, bus: EventBus) -> None:
        ev = make_event(EventType.HEALTH_CHECK, "monitor", {})
        bus._redis.lrange = AsyncMock(return_value=[ev.serialize()])
        events = await bus.replay(10)
        assert len(events) == 1
        assert events[0].event_type == EventType.HEALTH_CHECK
