"""Grafana dashboard JSON export."""

from __future__ import annotations

import json


def generate_dashboard_json() -> str:
    """Generate a Grafana dashboard JSON for CryptoForge."""
    dashboard = {
        "dashboard": {
            "title": "CryptoForge Trading Bot",
            "tags": ["crypto", "trading"],
            "timezone": "utc",
            "panels": [
                _panel_equity_curve(),
                _panel_daily_pnl(),
                _panel_drawdown(),
                _panel_trade_count(),
                _panel_win_rate(),
                _panel_circuit_breakers(),
                _panel_api_latency(),
                _panel_signals(),
            ],
            "time": {"from": "now-24h", "to": "now"},
            "refresh": "30s",
        }
    }
    return json.dumps(dashboard, indent=2)


def _panel_equity_curve() -> dict:
    return {
        "title": "Equity",
        "type": "timeseries",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "targets": [
            {
                "expr": "cryptoforge_equity_total",
                "legendFormat": "Equity",
            }
        ],
    }


def _panel_daily_pnl() -> dict:
    return {
        "title": "Daily P&L",
        "type": "stat",
        "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
        "targets": [{"expr": "cryptoforge_daily_pnl", "legendFormat": "P&L"}],
    }


def _panel_drawdown() -> dict:
    return {
        "title": "Drawdown %",
        "type": "gauge",
        "gridPos": {"h": 4, "w": 6, "x": 18, "y": 0},
        "targets": [{"expr": "cryptoforge_drawdown_pct"}],
        "fieldConfig": {
            "defaults": {"max": 20, "thresholds": {"steps": [
                {"value": 0, "color": "green"},
                {"value": 5, "color": "yellow"},
                {"value": 10, "color": "red"},
            ]}}
        },
    }


def _panel_trade_count() -> dict:
    return {
        "title": "Trades (24h)",
        "type": "stat",
        "gridPos": {"h": 4, "w": 6, "x": 12, "y": 4},
        "targets": [
            {"expr": 'increase(cryptoforge_trades_total[24h])', "legendFormat": "Trades"}
        ],
    }


def _panel_win_rate() -> dict:
    return {
        "title": "Win Rate (7d)",
        "type": "gauge",
        "gridPos": {"h": 4, "w": 6, "x": 18, "y": 4},
        "targets": [{"expr": "cryptoforge_win_rate_7d"}],
    }


def _panel_circuit_breakers() -> dict:
    return {
        "title": "Circuit Breakers",
        "type": "table",
        "gridPos": {"h": 4, "w": 12, "x": 0, "y": 8},
        "targets": [{"expr": "cryptoforge_circuit_breaker_status", "format": "table"}],
    }


def _panel_api_latency() -> dict:
    return {
        "title": "API Latency",
        "type": "timeseries",
        "gridPos": {"h": 6, "w": 12, "x": 0, "y": 12},
        "targets": [
            {
                "expr": "histogram_quantile(0.95, rate(cryptoforge_api_latency_seconds_bucket[5m]))",
                "legendFormat": "p95 {{endpoint}}",
            }
        ],
    }


def _panel_signals() -> dict:
    return {
        "title": "Signals Generated vs Rejected",
        "type": "timeseries",
        "gridPos": {"h": 6, "w": 12, "x": 12, "y": 12},
        "targets": [
            {"expr": "rate(cryptoforge_signals_generated_total[1h])", "legendFormat": "Generated {{strategy}}"},
            {"expr": "rate(cryptoforge_signals_rejected_total[1h])", "legendFormat": "Rejected {{strategy}}"},
        ],
    }
