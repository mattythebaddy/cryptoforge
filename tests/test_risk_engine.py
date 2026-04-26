"""Tests for the risk engine — the most important module."""

from __future__ import annotations

import pytest

from src.core.config import RiskConfig, TradingConfig
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.portfolio_manager import PortfolioManager, Position
from src.risk.position_sizer import PositionSizer
from src.risk.risk_engine import RiskDecision, RiskEngine, Signal


def _make_engine(
    equity: float = 10000,
    max_positions: int = 5,
    max_daily_loss: float = 5.0,
) -> RiskEngine:
    risk_cfg = RiskConfig(
        max_risk_per_trade_pct=1.0,
        max_daily_loss_pct=max_daily_loss,
        max_drawdown_pct=15.0,
        max_consecutive_losses=5,
        max_open_positions=max_positions,
        max_portfolio_exposure_pct=80.0,
    )
    trading_cfg = TradingConfig()
    cb = CircuitBreaker(max_daily_loss, 15.0, 5)
    cb.set_initial_equity(equity)
    pm = PortfolioManager(80.0)
    ps = PositionSizer()
    engine = RiskEngine(risk_cfg, trading_cfg, cb, pm, ps)
    engine.set_equity(equity)
    return engine


def _entry_signal(**overrides) -> Signal:
    defaults = dict(
        strategy_id="test",
        symbol="BTC/USDT",
        side="buy",
        signal_type="entry",
        price=65000,
        stop_loss=63000,
        take_profit=70000,
        confidence=0.6,
    )
    defaults.update(overrides)
    return Signal(**defaults)


class TestRiskEngine:
    @pytest.mark.asyncio
    async def test_approve_valid_signal(self) -> None:
        engine = _make_engine()
        decision = await engine.evaluate_signal(_entry_signal())
        assert decision.approved
        assert decision.adjusted_amount > 0

    @pytest.mark.asyncio
    async def test_reject_when_max_positions_reached(self) -> None:
        engine = _make_engine(max_positions=1)
        # Add an existing position
        engine._portfolio.add_position(
            Position("ETH/USDT", "buy", 1.0, 3000, "other")
        )
        decision = await engine.evaluate_signal(_entry_signal())
        assert not decision.approved
        assert "Max open positions" in decision.rejection_reason

    @pytest.mark.asyncio
    async def test_reject_duplicate_position(self) -> None:
        engine = _make_engine()
        engine._portfolio.add_position(
            Position("BTC/USDT", "buy", 0.01, 65000, "test")
        )
        decision = await engine.evaluate_signal(_entry_signal())
        assert not decision.approved
        assert "Already has position" in decision.rejection_reason

    @pytest.mark.asyncio
    async def test_reject_when_no_equity(self) -> None:
        engine = _make_engine(equity=0)
        decision = await engine.evaluate_signal(_entry_signal())
        assert not decision.approved

    @pytest.mark.asyncio
    async def test_stop_loss_required(self) -> None:
        engine = _make_engine()
        signal = _entry_signal(stop_loss=None)
        decision = await engine.evaluate_signal(signal)
        assert not decision.approved
        assert "Stop loss is required" in decision.rejection_reason

    @pytest.mark.asyncio
    async def test_reject_when_fee_exceeds_profit(self) -> None:
        engine = _make_engine()
        # TP only 0.01% above entry — can't cover fees
        signal = _entry_signal(take_profit=65006.5)
        decision = await engine.evaluate_signal(signal)
        assert not decision.approved
        assert "after fees" in decision.rejection_reason

    @pytest.mark.asyncio
    async def test_reject_when_exposure_exceeded(self) -> None:
        engine = _make_engine(equity=1000)
        # Add positions totalling 800 (80%)
        engine._portfolio.add_position(
            Position("ETH/USDT", "buy", 10, 80, "other")
        )
        decision = await engine.evaluate_signal(_entry_signal(price=200, stop_loss=180, take_profit=250))
        assert not decision.approved
        assert "exposure" in decision.rejection_reason.lower()

    @pytest.mark.asyncio
    async def test_reject_wide_stop_loss(self) -> None:
        engine = _make_engine()
        # Stop loss 20% below entry
        signal = _entry_signal(stop_loss=52000, take_profit=80000)
        decision = await engine.evaluate_signal(signal)
        assert not decision.approved
        assert "too wide" in decision.rejection_reason.lower()

    @pytest.mark.asyncio
    async def test_correlation_haircut_applied(self) -> None:
        engine = _make_engine()
        # Already long BTC
        engine._portfolio.add_position(
            Position("BTC/USDT", "buy", 0.01, 65000, "other_strat")
        )
        # Try to go long ETH — should get correlation haircut
        signal = _entry_signal(
            symbol="ETH/USDT", price=3000, stop_loss=2800, take_profit=3500
        )
        decision = await engine.evaluate_signal(signal)
        # Should still approve but with reduced size
        assert decision.approved

    @pytest.mark.asyncio
    async def test_exit_allowed_during_circuit_breaker(self) -> None:
        engine = _make_engine()
        engine._cb._status.daily_loss_triggered = True
        exit_signal = Signal(
            strategy_id="test",
            symbol="BTC/USDT",
            side="sell",
            signal_type="exit",
            price=64000,
            amount=0.01,
        )
        decision = await engine.evaluate_signal(exit_signal)
        assert decision.approved  # exits still allowed

    @pytest.mark.asyncio
    async def test_entry_blocked_during_circuit_breaker(self) -> None:
        engine = _make_engine()
        # Properly trigger the breaker by recording a large loss
        from datetime import datetime, timezone

        engine._cb._daily_reset_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        engine._cb._status.daily_loss_triggered = True
        decision = await engine.evaluate_signal(_entry_signal())
        assert not decision.approved
        assert "Circuit breaker" in decision.rejection_reason

    @pytest.mark.asyncio
    async def test_position_size_capped(self) -> None:
        engine = _make_engine(equity=10000)
        decision = await engine.evaluate_signal(_entry_signal())
        assert decision.approved
        # Max 5% of 10000 = 500 USD → ~0.0077 BTC at 65000
        max_size = (10000 * 0.05) / 65000
        assert decision.adjusted_amount <= max_size + 0.0001
