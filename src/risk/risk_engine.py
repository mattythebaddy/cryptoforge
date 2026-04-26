"""Central risk gate — ALL orders pass through this module. No exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.core.config import RiskConfig, TradingConfig
from src.core.event_bus import EventBus, EventType, make_event
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.portfolio_manager import PortfolioManager
from src.risk.position_sizer import PositionSizer

log = structlog.get_logger(__name__)


@dataclass
class Signal:
    """Signal emitted by a strategy. Passed to risk engine for approval."""

    strategy_id: str
    symbol: str
    side: str  # "buy" or "sell"
    signal_type: str  # "entry" or "exit"
    price: float
    amount: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    order_type: str = "limit"
    confidence: float = 0.5
    timeframe: str = "5m"
    reason: str = ""
    metadata: dict[str, Any] | None = None


@dataclass
class RiskDecision:
    """Result of risk evaluation."""

    approved: bool
    adjusted_amount: float = 0.0
    adjusted_stop_loss: float | None = None
    adjusted_take_profit: float | None = None
    rejection_reason: str = ""
    risk_score: float = 0.0


class RiskEngine:
    """
    The gatekeeper. Approves or rejects every trade signal.
    Has ABSOLUTE VETO POWER over all strategies.
    """

    def __init__(
        self,
        risk_config: RiskConfig,
        trading_config: TradingConfig,
        circuit_breaker: CircuitBreaker,
        portfolio_manager: PortfolioManager,
        position_sizer: PositionSizer,
        event_bus: EventBus | None = None,
    ) -> None:
        self._risk = risk_config
        self._trading = trading_config
        self._cb = circuit_breaker
        self._portfolio = portfolio_manager
        self._sizer = position_sizer
        self._event_bus = event_bus
        self._equity: float = 0.0
        self._peak_equity: float = 0.0
        self._capital_allocator: Any = None

    def set_equity(self, equity: float) -> None:
        self._equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def set_capital_allocator(self, allocator: Any) -> None:
        """Wire in the capital allocator for dynamic position sizing."""
        self._capital_allocator = allocator

    async def evaluate_signal(self, signal: Signal) -> RiskDecision:
        """Run ALL checks in order. Reject on first failure."""

        # --- CHECK 1: Circuit Breaker ---
        cb_status = self._cb.check()
        if cb_status.any_triggered:
            # Allow exits even when breakers are active
            if signal.signal_type == "entry":
                reason = f"Circuit breaker active: {cb_status.summary()}"
                return await self._reject(signal, reason)

        # --- CHECK 2: Position Limits ---
        if signal.signal_type == "entry":
            if self._portfolio.open_count >= self._risk.max_open_positions:
                return await self._reject(
                    signal,
                    f"Max open positions ({self._risk.max_open_positions}) reached",
                )

            if self._portfolio.has_position(signal.symbol, signal.strategy_id):
                return await self._reject(
                    signal,
                    f"Already has position in {signal.symbol} for {signal.strategy_id}",
                )

        # --- CHECK 3: Position Sizing ---
        if signal.signal_type == "entry":
            if self._equity <= 0:
                return await self._reject(signal, "No equity available")

            # Calculate size
            stop_loss = signal.stop_loss
            if stop_loss is None:
                return await self._reject(signal, "Stop loss is required for entry signals")

            amount = self._sizer.calculate(
                method=self._risk.position_sizing_method,
                equity=self._equity,
                entry_price=signal.price,
                stop_loss_price=stop_loss,
                risk_pct=self._risk.max_risk_per_trade_pct,
                win_rate=0.5 + signal.confidence * 0.2,  # map confidence to win rate
            )

            if amount <= 0:
                return await self._reject(signal, "Calculated position size is zero")

            # Override amount if signal specified one (but cap it)
            if signal.amount is not None:
                amount = min(signal.amount, amount)

            # Apply capital allocator multiplier (optimizer)
            if self._capital_allocator is not None:
                mult = self._capital_allocator.get_multiplier(signal.strategy_id)
                if abs(mult - 1.0) > 0.01:
                    amount *= mult
                    log.info(
                        "risk.capital_multiplier",
                        strategy=signal.strategy_id,
                        multiplier=round(mult, 2),
                        adjusted_amount=round(amount, 8),
                    )

            # Drawdown-based position scaling — reduce size when in drawdown
            if self._peak_equity > 0:
                dd_pct = (self._peak_equity - self._equity) / self._peak_equity * 100
                if dd_pct > 3.0:
                    # Linear scale: 1.0 at 3%, 0.5 at 10%, 0.3 at 17%
                    dd_scaler = max(0.3, 1.0 - (dd_pct - 3.0) / 14.0)
                    amount *= dd_scaler
                    log.info(
                        "risk.drawdown_scaler",
                        drawdown_pct=round(dd_pct, 2),
                        scaler=round(dd_scaler, 2),
                        adjusted_amount=round(amount, 8),
                    )

        else:
            amount = signal.amount or 0

        # --- CHECK 4: Fee Viability ---
        if signal.signal_type == "entry" and signal.take_profit is not None:
            round_trip_cost = (
                self._trading.maker_fee_pct
                + self._trading.taker_fee_pct
                + 2 * self._trading.estimated_slippage_pct
            ) / 100

            expected_profit_pct = abs(signal.take_profit - signal.price) / signal.price
            # Must at least cover round-trip fees, or meet the configured threshold
            min_profit = max(round_trip_cost, self._trading.min_profit_threshold_pct / 100)

            if expected_profit_pct < min_profit:
                return await self._reject(
                    signal,
                    f"Expected profit {expected_profit_pct*100:.3f}% < minimum {min_profit*100:.3f}% after fees",
                )

        # --- CHECK 5: Portfolio Exposure ---
        if signal.signal_type == "entry":
            notional = amount * signal.price
            if self._portfolio.would_exceed_exposure(notional, self._equity):
                return await self._reject(
                    signal,
                    f"Would exceed max exposure ({self._risk.max_portfolio_exposure_pct}%)",
                )

            # Correlation haircut
            haircut = self._portfolio.correlation_haircut(signal.symbol, signal.side)
            if haircut < 1.0:
                amount *= haircut
                log.info(
                    "risk.correlation_haircut",
                    symbol=signal.symbol,
                    haircut=round(haircut, 2),
                    new_amount=round(amount, 8),
                )

        # --- CHECK 6: Sanity Checks ---
        if signal.signal_type == "entry":
            # Stop loss not wider than 5x ATR equivalent (rough 5% cap)
            if signal.stop_loss is not None:
                stop_distance_pct = abs(signal.price - signal.stop_loss) / signal.price * 100
                if stop_distance_pct > 10:
                    return await self._reject(
                        signal,
                        f"Stop loss distance {stop_distance_pct:.1f}% too wide (max 10%)",
                    )

            # Position risk check
            risk_amount = amount * abs(signal.price - (signal.stop_loss or signal.price))
            risk_pct = (risk_amount / self._equity * 100) if self._equity > 0 else 100
            if risk_pct > self._risk.max_risk_per_trade_pct * 1.5:
                return await self._reject(
                    signal,
                    f"Trade risk {risk_pct:.2f}% exceeds limit {self._risk.max_risk_per_trade_pct}%",
                )

        # --- APPROVED ---
        decision = RiskDecision(
            approved=True,
            adjusted_amount=amount,
            adjusted_stop_loss=signal.stop_loss,
            adjusted_take_profit=signal.take_profit,
            risk_score=self._calculate_risk_score(signal, amount),
        )

        log.info(
            "risk.approved",
            symbol=signal.symbol,
            side=signal.side,
            amount=round(amount, 8),
            strategy=signal.strategy_id,
            risk_score=round(decision.risk_score, 2),
        )

        if self._event_bus:
            await self._event_bus.publish(
                make_event(
                    EventType.RISK_APPROVED,
                    "risk_engine",
                    {
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "amount": amount,
                        "strategy": signal.strategy_id,
                    },
                )
            )

        return decision

    async def _reject(self, signal: Signal, reason: str) -> RiskDecision:
        log.warning(
            "risk.rejected",
            symbol=signal.symbol,
            side=signal.side,
            strategy=signal.strategy_id,
            reason=reason,
        )
        if self._event_bus:
            await self._event_bus.publish(
                make_event(
                    EventType.RISK_REJECTED,
                    "risk_engine",
                    {
                        "symbol": signal.symbol,
                        "strategy": signal.strategy_id,
                        "reason": reason,
                    },
                )
            )
        return RiskDecision(approved=False, rejection_reason=reason)

    def _calculate_risk_score(self, signal: Signal, amount: float) -> float:
        """0-1 risk score: higher = riskier."""
        score = 0.0
        # Position size relative to equity
        if self._equity > 0:
            notional_pct = (amount * signal.price) / self._equity * 100
            score += min(notional_pct / 20, 0.4)  # cap contribution at 0.4
        # Portfolio exposure
        exp_pct = self._portfolio.exposure_pct(self._equity)
        score += min(exp_pct / 200, 0.3)
        # Circuit breaker proximity
        cb = self._cb.get_state()
        if cb.get("consecutive_losses", 0) > 0:
            score += 0.1 * cb["consecutive_losses"] / self._risk.max_consecutive_losses
        return min(score, 1.0)
