"""Automatic kill switches to prevent catastrophic losses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from src.core.event_bus import EventBus, EventType, make_event

log = structlog.get_logger(__name__)


@dataclass
class CircuitBreakerStatus:
    daily_loss_triggered: bool = False
    drawdown_triggered: bool = False
    consecutive_loss_triggered: bool = False
    volatility_triggered: bool = False
    btc_crash_triggered: bool = False

    @property
    def any_triggered(self) -> bool:
        return any(
            [
                self.daily_loss_triggered,
                self.drawdown_triggered,
                self.consecutive_loss_triggered,
                self.volatility_triggered,
                self.btc_crash_triggered,
            ]
        )

    @property
    def can_trade(self) -> bool:
        return not self.any_triggered

    def summary(self) -> dict[str, bool]:
        return {
            "daily_loss": self.daily_loss_triggered,
            "drawdown": self.drawdown_triggered,
            "consecutive_loss": self.consecutive_loss_triggered,
            "volatility": self.volatility_triggered,
            "btc_crash": self.btc_crash_triggered,
        }


class CircuitBreaker:
    """
    Automatic kill switches. State persisted to Redis.
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 5.0,
        max_drawdown_pct: float = 15.0,
        max_consecutive_losses: int = 5,
        event_bus: EventBus | None = None,
    ) -> None:
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_drawdown_pct = max_drawdown_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._event_bus = event_bus

        # State
        self._daily_pnl: float = 0.0
        self._daily_reset_date: str = ""
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0
        self._consecutive_losses: int = 0
        self._status = CircuitBreakerStatus()

        # Timers
        self._volatility_resume_at: float = 0
        self._consecutive_resume_at: float = 0

    def check(self) -> CircuitBreakerStatus:
        """Return current status. Check time-based auto-resets."""
        now = asyncio.get_event_loop().time()

        # Auto-reset: daily loss at UTC midnight
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
            self._status.daily_loss_triggered = False

        # Auto-reset: volatility breaker (30 min)
        if self._status.volatility_triggered and now > self._volatility_resume_at:
            self._status.volatility_triggered = False
            log.info("circuit_breaker.volatility_reset")

        # Auto-reset: consecutive loss (4 hours)
        if self._status.consecutive_loss_triggered and now > self._consecutive_resume_at:
            self._status.consecutive_loss_triggered = False
            log.info("circuit_breaker.consecutive_reset")

        return self._status

    async def record_trade_result(self, pnl: float, equity: float) -> None:
        """Called after every trade closes. Updates all breaker states."""
        self._current_equity = equity

        # Track peak
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Ensure daily reset date is set
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not self._daily_reset_date:
            self._daily_reset_date = today
        elif today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
            self._status.daily_loss_triggered = False

        # 1. DAILY LOSS
        self._daily_pnl += pnl
        if self._peak_equity > 0:
            daily_loss_pct = abs(self._daily_pnl) / self._peak_equity * 100
            if self._daily_pnl < 0 and daily_loss_pct > self._max_daily_loss_pct:
                self._status.daily_loss_triggered = True
                log.warning(
                    "circuit_breaker.daily_loss",
                    daily_pnl=round(self._daily_pnl, 2),
                    pct=round(daily_loss_pct, 2),
                )
                await self._emit("daily_loss", f"Daily loss: {daily_loss_pct:.1f}%")

        # 2. DRAWDOWN
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100
            if drawdown_pct > self._max_drawdown_pct:
                self._status.drawdown_triggered = True
                log.warning(
                    "circuit_breaker.drawdown",
                    drawdown_pct=round(drawdown_pct, 2),
                    peak=round(self._peak_equity, 2),
                    current=round(equity, 2),
                )
                await self._emit("drawdown", f"Drawdown: {drawdown_pct:.1f}%")

        # 3. CONSECUTIVE LOSSES
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._max_consecutive_losses:
                self._status.consecutive_loss_triggered = True
                self._consecutive_resume_at = asyncio.get_event_loop().time() + 4 * 3600
                log.warning(
                    "circuit_breaker.consecutive",
                    losses=self._consecutive_losses,
                )
                await self._emit(
                    "consecutive_loss",
                    f"{self._consecutive_losses} consecutive losses",
                )
        else:
            self._consecutive_losses = 0

    async def record_volatility_spike(self, change_pct: float) -> None:
        """Call when a 5-min candle moves > 5%."""
        if abs(change_pct) > 5.0:
            self._status.volatility_triggered = True
            self._volatility_resume_at = asyncio.get_event_loop().time() + 30 * 60
            log.warning("circuit_breaker.volatility", change_pct=round(change_pct, 2))
            await self._emit("volatility", f"{change_pct:.1f}% move in 5 minutes")

    async def record_btc_crash(self, change_24h_pct: float) -> None:
        """Call when BTC drops > 10% in 24h."""
        if change_24h_pct < -10:
            self._status.btc_crash_triggered = True
            log.warning("circuit_breaker.btc_crash", change=round(change_24h_pct, 2))
            await self._emit("btc_crash", f"BTC {change_24h_pct:.1f}% in 24h")

    def reset_drawdown(self) -> None:
        """Manual reset after review (Telegram /reset_drawdown command)."""
        self._status.drawdown_triggered = False
        self._peak_equity = self._current_equity
        log.info("circuit_breaker.drawdown_reset")

    def reset_btc_crash(self) -> None:
        self._status.btc_crash_triggered = False
        log.info("circuit_breaker.btc_crash_reset")

    def set_initial_equity(self, equity: float) -> None:
        if self._peak_equity == 0:
            self._peak_equity = equity
        self._current_equity = equity

    def get_state(self) -> dict:
        return {
            "daily_pnl": self._daily_pnl,
            "daily_reset_date": self._daily_reset_date,
            "peak_equity": self._peak_equity,
            "current_equity": self._current_equity,
            "consecutive_losses": self._consecutive_losses,
            "status": self._status.summary(),
        }

    def load_state(self, state: dict) -> None:
        self._daily_pnl = state.get("daily_pnl", 0)
        self._daily_reset_date = state.get("daily_reset_date", "")
        self._peak_equity = state.get("peak_equity", 0)
        self._current_equity = state.get("current_equity", 0)
        self._consecutive_losses = state.get("consecutive_losses", 0)
        status = state.get("status", {})
        self._status.daily_loss_triggered = status.get("daily_loss", False)
        self._status.drawdown_triggered = status.get("drawdown", False)
        self._status.consecutive_loss_triggered = status.get("consecutive_loss", False)

    async def _emit(self, breaker: str, details: str) -> None:
        if self._event_bus:
            await self._event_bus.publish(
                make_event(
                    EventType.CIRCUIT_BREAKER_TRIGGERED,
                    "circuit_breaker",
                    {"breaker": breaker, "details": details},
                )
            )
