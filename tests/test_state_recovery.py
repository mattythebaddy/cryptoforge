"""Tests for state recovery after crash."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import orjson
import pytest

from src.core.state_manager import StateManager


class TestCrashRecovery:
    @pytest.fixture
    def sm(self, mock_redis: AsyncMock) -> StateManager:
        sm = StateManager("redis://localhost:6379/0")
        sm._redis = mock_redis
        return sm

    @pytest.mark.asyncio
    async def test_state_survives_save_reload(self, sm: StateManager) -> None:
        """Verify state can be saved and loaded back."""
        positions = {
            "BTC/USDT": {"side": "long", "size": 0.01, "entry": 65000},
            "ETH/USDT": {"side": "long", "size": 1.0, "entry": 3000},
        }

        with patch("src.core.state_manager.get_session"):
            await sm.save_state("positions", positions)

        # Simulate redis returning the data
        sm._redis.get = AsyncMock(return_value=orjson.dumps(positions))
        loaded = await sm.load_state("positions")
        assert loaded == positions
        assert loaded["BTC/USDT"]["entry"] == 65000

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_persists(self, sm: StateManager) -> None:
        cb_state = {
            "daily_pnl": -300,
            "consecutive_losses": 4,
            "peak_equity": 10000,
            "current_equity": 9700,
        }

        with patch("src.core.state_manager.get_session"):
            await sm.save_state("circuit_breaker", cb_state)

        sm._redis.get = AsyncMock(return_value=orjson.dumps(cb_state))
        loaded = await sm.load_state("circuit_breaker")
        assert loaded["consecutive_losses"] == 4
        assert loaded["daily_pnl"] == -300

    @pytest.mark.asyncio
    async def test_strategy_state_persists(self, sm: StateManager) -> None:
        strat_state = {
            "grid_trading": {"round_trips": 15, "total_profit": 234.56},
            "momentum": {"in_position": True, "entry_price": 65000},
        }

        with patch("src.core.state_manager.get_session"):
            await sm.save_state("strategy_states", strat_state)

        sm._redis.get = AsyncMock(return_value=orjson.dumps(strat_state))
        loaded = await sm.load_state("strategy_states")
        assert loaded["grid_trading"]["round_trips"] == 15
        assert loaded["momentum"]["in_position"] is True
