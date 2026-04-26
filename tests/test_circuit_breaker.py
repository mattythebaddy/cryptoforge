"""Tests for circuit breaker."""

from __future__ import annotations

import pytest

from src.risk.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    @pytest.fixture
    def cb(self) -> CircuitBreaker:
        cb = CircuitBreaker(max_daily_loss_pct=5.0, max_drawdown_pct=15.0, max_consecutive_losses=3)
        cb.set_initial_equity(10000)
        return cb

    @pytest.mark.asyncio
    async def test_no_trigger_on_winning_trade(self, cb: CircuitBreaker) -> None:
        await cb.record_trade_result(100, 10100)
        status = cb.check()
        assert not status.any_triggered

    @pytest.mark.asyncio
    async def test_daily_loss_trigger(self, cb: CircuitBreaker) -> None:
        # Lose 6% of equity in one day
        await cb.record_trade_result(-600, 9400)
        status = cb.check()
        assert status.daily_loss_triggered

    @pytest.mark.asyncio
    async def test_drawdown_trigger(self, cb: CircuitBreaker) -> None:
        cb.set_initial_equity(10000)
        # Drop to 8400 (16% drawdown)
        await cb.record_trade_result(-1600, 8400)
        status = cb.check()
        assert status.drawdown_triggered

    @pytest.mark.asyncio
    async def test_consecutive_loss_trigger(self, cb: CircuitBreaker) -> None:
        for i in range(3):
            await cb.record_trade_result(-50, 10000 - 50 * (i + 1))
        status = cb.check()
        assert status.consecutive_loss_triggered

    @pytest.mark.asyncio
    async def test_consecutive_reset_on_win(self, cb: CircuitBreaker) -> None:
        await cb.record_trade_result(-50, 9950)
        await cb.record_trade_result(-50, 9900)
        await cb.record_trade_result(100, 10000)  # win resets counter
        assert cb._consecutive_losses == 0

    @pytest.mark.asyncio
    async def test_volatility_trigger(self, cb: CircuitBreaker) -> None:
        await cb.record_volatility_spike(6.0)
        status = cb.check()
        assert status.volatility_triggered

    @pytest.mark.asyncio
    async def test_btc_crash_trigger(self, cb: CircuitBreaker) -> None:
        await cb.record_btc_crash(-12.0)
        status = cb.check()
        assert status.btc_crash_triggered

    def test_manual_drawdown_reset(self, cb: CircuitBreaker) -> None:
        cb._status.drawdown_triggered = True
        cb.reset_drawdown()
        assert not cb._status.drawdown_triggered

    def test_state_persistence(self, cb: CircuitBreaker) -> None:
        cb._daily_pnl = -300
        cb._consecutive_losses = 2
        state = cb.get_state()

        new_cb = CircuitBreaker()
        new_cb.load_state(state)
        assert new_cb._daily_pnl == -300
        assert new_cb._consecutive_losses == 2

    @pytest.mark.asyncio
    async def test_can_trade_when_healthy(self, cb: CircuitBreaker) -> None:
        status = cb.check()
        assert status.can_trade
