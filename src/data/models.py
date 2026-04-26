"""SQLAlchemy 2.0 async ORM models for TimescaleDB."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import structlog

log = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# OHLCV candle data (primary time-series)
# ---------------------------------------------------------------------------

class OHLCV(Base):
    __tablename__ = "ohlcv"

    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("time", "symbol", "timeframe", name="idx_ohlcv_unique"),
        Index("idx_ohlcv_symbol_time", "symbol", time.desc()),
    )


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    fee_paid: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(128))
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_type: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[dict] = mapped_column(JSONB, default=dict)


# ---------------------------------------------------------------------------
# Account snapshots (equity curve)
# ---------------------------------------------------------------------------

class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    total_equity: Mapped[float] = mapped_column(Float, nullable=False)
    free_balance: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0)
    positions_value: Mapped[float] = mapped_column(Float, default=0)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0)
    peak_equity: Mapped[float] = mapped_column(Float, nullable=False)


# ---------------------------------------------------------------------------
# Strategy performance tracking
# ---------------------------------------------------------------------------

class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"

    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    total_trades: Mapped[int] = mapped_column(BigInteger, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0)
    sharpe_ratio: Mapped[float] = mapped_column(Float, default=0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0)
    avg_trade_pnl: Mapped[float] = mapped_column(Float, default=0)
    expectancy: Mapped[float] = mapped_column(Float, default=0)


# ---------------------------------------------------------------------------
# Bot state persistence (crash recovery backup)
# ---------------------------------------------------------------------------

class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(url: str) -> AsyncEngine:
    """Create engine, tables, and TimescaleDB hypertables."""
    global _engine, _session_factory

    _engine = create_async_engine(url, echo=False, pool_size=5, max_overflow=10)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # TimescaleDB hypertables (idempotent with if_not_exists)
        for table, col in [
            ("ohlcv", "time"),
            ("account_snapshots", "time"),
            ("strategy_performance", "time"),
        ]:
            try:
                await conn.execute(
                    text(
                        f"SELECT create_hypertable('{table}', '{col}', "
                        f"if_not_exists => TRUE, migrate_data => TRUE)"
                    )
                )
            except Exception as exc:
                # TimescaleDB extension may not be available in test environments
                log.warning("hypertable_skip", table=table, error=str(exc))

        # Compression policy for OHLCV
        try:
            await conn.execute(
                text(
                    "ALTER TABLE ohlcv SET ("
                    "  timescaledb.compress,"
                    "  timescaledb.compress_segmentby = 'symbol,timeframe',"
                    "  timescaledb.compress_orderby = 'time DESC'"
                    ")"
                )
            )
            await conn.execute(
                text("SELECT add_compression_policy('ohlcv', INTERVAL '7 days', if_not_exists => TRUE)")
            )
        except Exception as exc:
            log.warning("compression_skip", error=str(exc))

    log.info("database.initialized", url=url.split("@")[-1])  # mask creds
    return _engine


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _session_factory()


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _engine
