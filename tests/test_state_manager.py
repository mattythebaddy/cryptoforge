"""Tests for the state manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import orjson
import pytest

from src.core.state_manager import StateManager


class TestStateManager:
    @pytest.fixture
    def sm(self, mock_redis: AsyncMock) -> StateManager:
        sm = StateManager("redis://localhost:6379/0")
        sm._redis = mock_redis
        return sm

    @pytest.mark.asyncio
    async def test_save_and_load_from_redis(self, sm: StateManager) -> None:
        state = {"positions": [{"symbol": "BTC/USDT", "side": "long"}]}
        payload = orjson.dumps(state)

        # Patch get_session so PG backup doesn't try real connection
        with patch("src.core.state_manager.get_session"):
            await sm.save_state("positions", state)

        sm._redis.set.assert_called_once()

        # Simulate redis returning the saved value
        sm._redis.get = AsyncMock(return_value=payload)
        loaded = await sm.load_state("positions")
        assert loaded == state

    @pytest.mark.asyncio
    async def test_load_returns_none_when_missing(self, sm: StateManager) -> None:
        sm._redis.get = AsyncMock(return_value=None)
        # PG also returns nothing
        with patch("src.core.state_manager.get_session") as mock_sess:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            # Make the execute return no rows
            session = AsyncMock()
            result = AsyncMock()
            result.scalar_one_or_none = lambda: None
            session.execute = AsyncMock(return_value=result)
            mock_sess.return_value = session
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)

            loaded = await sm.load_state("nonexistent")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_state(self, sm: StateManager) -> None:
        with patch("src.core.state_manager.get_session") as mock_sess:
            session = AsyncMock()
            session.get = AsyncMock(return_value=None)
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            mock_sess.return_value = session

            await sm.delete_state("old_key")
        sm._redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_keys(self, sm: StateManager) -> None:
        sm._redis.keys = AsyncMock(
            return_value=[b"cryptoforge:state:positions", b"cryptoforge:state:daily_pnl"]
        )
        keys = await sm.list_keys()
        assert keys == ["positions", "daily_pnl"]

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        sm = StateManager("redis://localhost:6379/0")
        with pytest.raises(RuntimeError, match="not connected"):
            await sm.save_state("k", {})


class TestReconciliation:
    @pytest.mark.asyncio
    async def test_reconcile_without_exchange(self, mock_redis: AsyncMock) -> None:
        sm = StateManager("redis://localhost:6379/0")
        sm._redis = mock_redis

        with patch("src.core.state_manager.get_session") as mock_sess:
            session = AsyncMock()
            result = AsyncMock()
            result.scalars = lambda: AsyncMock(all=lambda: [])
            session.execute = AsyncMock(return_value=result)
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            mock_sess.return_value = session

            report = await sm.reconcile_with_exchange()

        assert not report.has_discrepancies
