"""Prometheus metrics exporter."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import structlog

log = structlog.get_logger(__name__)

# --- Trading metrics ---
TRADES_TOTAL = Counter(
    "cryptoforge_trades_total",
    "Total trades executed",
    ["symbol", "side", "strategy", "result"],
)
PNL_TOTAL = Gauge(
    "cryptoforge_pnl_total",
    "Cumulative P&L",
    ["symbol", "strategy"],
)
DAILY_PNL = Gauge("cryptoforge_daily_pnl", "Daily P&L in base currency")
DRAWDOWN_PCT = Gauge("cryptoforge_drawdown_pct", "Current drawdown percentage")
EQUITY_TOTAL = Gauge("cryptoforge_equity_total", "Total account equity")
POSITION_VALUE = Gauge(
    "cryptoforge_position_value", "Position value by symbol", ["symbol"]
)
UNREALIZED_PNL = Gauge(
    "cryptoforge_unrealized_pnl", "Unrealized P&L by symbol", ["symbol"]
)

# --- System metrics ---
WS_CONNECTED = Gauge(
    "cryptoforge_websocket_connected",
    "WebSocket connection status",
    ["exchange"],
)
API_LATENCY = Histogram(
    "cryptoforge_api_latency_seconds",
    "Exchange API latency",
    ["exchange", "endpoint"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
ORDER_LATENCY = Histogram(
    "cryptoforge_order_latency_seconds",
    "Order placement latency",
    ["exchange"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
CANDLES_PROCESSED = Counter(
    "cryptoforge_candles_processed_total", "Total candles processed"
)
SIGNALS_GENERATED = Counter(
    "cryptoforge_signals_generated_total",
    "Total signals generated",
    ["strategy"],
)
SIGNALS_REJECTED = Counter(
    "cryptoforge_signals_rejected_total",
    "Total signals rejected by risk engine",
    ["strategy", "reason"],
)
ERRORS_TOTAL = Counter(
    "cryptoforge_errors_total",
    "Total errors",
    ["module", "error_type"],
)

# --- Risk metrics ---
CIRCUIT_BREAKER = Gauge(
    "cryptoforge_circuit_breaker_status",
    "Circuit breaker status (0=off, 1=on)",
    ["breaker_name"],
)
DAILY_LOSS_PCT = Gauge("cryptoforge_daily_loss_pct", "Daily loss percentage")
CONSECUTIVE_LOSSES = Gauge(
    "cryptoforge_consecutive_losses", "Consecutive losing trades"
)
WIN_RATE_7D = Gauge("cryptoforge_win_rate_7d", "7-day rolling win rate")
SHARPE_30D = Gauge("cryptoforge_sharpe_ratio_30d", "30-day rolling Sharpe ratio")


def start_metrics_server(port: int = 9090) -> None:
    """Start Prometheus HTTP metrics server."""
    start_http_server(port)
    log.info("metrics.started", port=port)
