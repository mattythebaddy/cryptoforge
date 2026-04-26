"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Write a minimal valid config YAML to a temp file."""
    cfg = tmp_path / "test_config.yaml"
    cfg.write_text(
        """\
exchange:
  name: binance
  testnet: true

trading:
  mode: paper
  trading_pairs:
    - BTC/USDT

risk:
  max_risk_per_trade_pct: 1.0
  max_daily_loss_pct: 5.0
  max_drawdown_pct: 15.0

telegram:
  bot_token: ""
  chat_id: ""

database:
  timescaledb_url: postgresql+asyncpg://bot:password@localhost:5432/cryptoforge_test
  redis_url: redis://localhost:6379/1

logging:
  level: DEBUG
  json_output: false
  log_dir: logs
"""
    )
    return cfg


@pytest.fixture
def mock_redis() -> AsyncMock:
    """A mocked redis.asyncio.Redis instance."""
    r = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.keys = AsyncMock(return_value=[])
    r.lpush = AsyncMock()
    r.ltrim = AsyncMock()
    r.lrange = AsyncMock(return_value=[])
    r.publish = AsyncMock()
    r.close = AsyncMock()
    return r
