"""Composite health scoring for the entire system."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class HealthReport:
    score: int = 0  # 0-100
    exchange_connectivity: int = 0  # 0-25
    data_freshness: int = 0  # 0-25
    strategy_health: int = 0  # 0-25
    risk_system: int = 0  # 0-25
    details: dict[str, Any] | None = None
    action: str = "normal"  # normal, warning, degraded, critical


class HealthChecker:
    """Composite health check for the entire system."""

    def __init__(
        self,
        exchange_client: Any = None,
        event_bus: Any = None,
        circuit_breaker: Any = None,
        strategy_manager: Any = None,
    ) -> None:
        self._exchange = exchange_client
        self._event_bus = event_bus
        self._circuit_breaker = circuit_breaker
        self._strategy_manager = strategy_manager
        self._last_candle_time: float = 0

    def set_last_candle_time(self, t: float) -> None:
        self._last_candle_time = t

    async def check(self) -> HealthReport:
        report = HealthReport(details={})

        # 1. Exchange Connectivity (25 pts)
        report.exchange_connectivity = await self._check_exchange()

        # 2. Data Freshness (25 pts)
        report.data_freshness = self._check_data()

        # 3. Strategy Health (25 pts)
        report.strategy_health = self._check_strategies()

        # 4. Risk System (25 pts)
        report.risk_system = self._check_risk()

        report.score = (
            report.exchange_connectivity
            + report.data_freshness
            + report.strategy_health
            + report.risk_system
        )

        # Graduated response
        if report.score >= 80:
            report.action = "normal"
        elif report.score >= 60:
            report.action = "warning"
        elif report.score >= 40:
            report.action = "degraded"
        else:
            report.action = "critical"

        log.info(
            "health.check",
            score=report.score,
            action=report.action,
            exchange=report.exchange_connectivity,
            data=report.data_freshness,
            strategy=report.strategy_health,
            risk=report.risk_system,
        )

        return report

    async def _check_exchange(self) -> int:
        pts = 0
        if not self._exchange:
            return 0
        try:
            # Can we reach the exchange?
            await self._exchange.get_balance()
            pts += 10  # connected
            pts += 10  # fast response (we got here)
            pts += 5   # no rate limit issues (assumed if success)
        except Exception:
            log.warning("health.exchange_unreachable")
        return pts

    def _check_data(self) -> int:
        pts = 0
        now = asyncio.get_event_loop().time()
        gap = now - self._last_candle_time if self._last_candle_time > 0 else float("inf")

        if gap < 120:  # candle < 2 min old
            pts += 10
        if gap < 300:  # reasonably fresh
            pts += 10
        if gap < 600:
            pts += 5

        return pts

    def _check_strategies(self) -> int:
        pts = 0
        if not self._strategy_manager:
            return 15  # assume OK if not connected
        active = self._strategy_manager.get_active_strategies()
        if active:
            pts += 10
        pts += 10  # no error tracking yet
        pts += 5
        return min(pts, 25)

    def _check_risk(self) -> int:
        pts = 0
        if not self._circuit_breaker:
            return 15
        status = self._circuit_breaker.check()
        if not status.any_triggered:
            pts += 10
        pts += 10  # drawdown within limit (assumed)
        pts += 5
        return min(pts, 25)
