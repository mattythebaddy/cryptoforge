"""Persistent state manager with Redis primary and PostgreSQL backup."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import orjson
import redis.asyncio as aioredis
import structlog
from sqlalchemy import select

from src.core.exceptions import StateRecoveryError
from src.data.models import BotState, get_session

log = structlog.get_logger(__name__)

_PREFIX = "cryptoforge:state:"


class ReconciliationReport:
    """Summary of state reconciliation after a restart."""

    def __init__(self) -> None:
        self.orphaned_orders_cancelled: list[str] = []
        self.positions_synced: int = 0
        self.state_keys_loaded: int = 0
        self.discrepancies: list[str] = []

    @property
    def has_discrepancies(self) -> bool:
        return len(self.discrepancies) > 0

    def summary(self) -> str:
        parts = [
            f"keys_loaded={self.state_keys_loaded}",
            f"positions_synced={self.positions_synced}",
            f"orphaned_cancelled={len(self.orphaned_orders_cancelled)}",
            f"discrepancies={len(self.discrepancies)}",
        ]
        return ", ".join(parts)


class StateManager:
    """
    Persists bot state to survive crashes.
    Redis is the hot store; PostgreSQL is the durable backup.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=False)
        await self._redis.ping()
        log.info("state_manager.connected")

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    # -- core ops --

    async def save_state(self, key: str, state: dict[str, Any]) -> None:
        """Atomic save to Redis + async write to PostgreSQL."""
        if not self._redis:
            raise RuntimeError("StateManager not connected")

        payload = orjson.dumps(state)

        # Redis (primary)
        await self._redis.set(f"{_PREFIX}{key}", payload)

        # PostgreSQL (backup)
        try:
            async with get_session() as session:
                existing = await session.get(BotState, key)
                if existing:
                    existing.value = state
                    existing.updated_at = datetime.now(timezone.utc)
                else:
                    session.add(BotState(key=key, value=state))
                await session.commit()
        except Exception:
            log.exception("state_manager.pg_backup_failed", key=key)

    async def load_state(self, key: str) -> dict[str, Any] | None:
        """Load from Redis first, fall back to PostgreSQL."""
        if not self._redis:
            raise RuntimeError("StateManager not connected")

        # try Redis
        raw = await self._redis.get(f"{_PREFIX}{key}")
        if raw:
            return orjson.loads(raw)

        # fall back to PostgreSQL
        try:
            async with get_session() as session:
                result = await session.execute(select(BotState).where(BotState.key == key))
                row = result.scalar_one_or_none()
                if row:
                    # re-populate Redis cache
                    await self._redis.set(f"{_PREFIX}{key}", orjson.dumps(row.value))
                    return row.value
        except Exception:
            log.exception("state_manager.pg_load_failed", key=key)

        return None

    async def delete_state(self, key: str) -> None:
        if self._redis:
            await self._redis.delete(f"{_PREFIX}{key}")
        try:
            async with get_session() as session:
                existing = await session.get(BotState, key)
                if existing:
                    await session.delete(existing)
                    await session.commit()
        except Exception:
            log.exception("state_manager.delete_failed", key=key)

    async def list_keys(self) -> list[str]:
        """List all persisted state keys."""
        if not self._redis:
            return []
        keys = await self._redis.keys(f"{_PREFIX}*")
        return [k.decode().removeprefix(_PREFIX) for k in keys]

    async def checkpoint(self) -> None:
        """Save full system snapshot — called after every trade."""
        keys = await self.list_keys()
        for key in keys:
            raw = await self._redis.get(f"{_PREFIX}{key}")  # type: ignore[union-attr]
            if raw:
                state = orjson.loads(raw)
                try:
                    async with get_session() as session:
                        existing = await session.get(BotState, key)
                        if existing:
                            existing.value = state
                            existing.updated_at = datetime.now(timezone.utc)
                        else:
                            session.add(BotState(key=key, value=state))
                        await session.commit()
                except Exception:
                    log.exception("state_manager.checkpoint_failed", key=key)
        log.debug("state_manager.checkpoint_complete", keys=len(keys))

    async def reconcile_with_exchange(self, exchange_client: Any = None) -> ReconciliationReport:
        """
        Called on every startup. Compares local state with exchange.
        Phase 1 stub — full implementation in Phase 2 when ExchangeClient exists.
        """
        report = ReconciliationReport()

        if exchange_client is None:
            log.info("state_manager.reconcile_skipped", reason="no_exchange_client")
            # Load keys from PG into Redis if Redis was wiped
            try:
                async with get_session() as session:
                    result = await session.execute(select(BotState))
                    rows = result.scalars().all()
                    for row in rows:
                        await self._redis.set(  # type: ignore[union-attr]
                            f"{_PREFIX}{row.key}", orjson.dumps(row.value)
                        )
                        report.state_keys_loaded += 1
            except Exception:
                log.exception("state_manager.reconcile_pg_load_failed")
                raise StateRecoveryError("Failed to load state from PostgreSQL")

            log.info("state_manager.reconciled", summary=report.summary())
            return report

        # TODO: Phase 2 — fetch open orders + positions from exchange,
        #   compare with local state, cancel orphans, sync state.
        log.info("state_manager.reconciled", summary=report.summary())
        return report
