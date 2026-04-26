"""Pydantic v2 configuration with YAML loading and .env override."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.exceptions import ConfigError


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ExchangeConfig(BaseModel):
    name: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    rate_limit: bool = True
    timeout: int = 30000
    options: dict[str, Any] = Field(default_factory=dict)


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 5
    max_leverage: float = 3.0
    max_open_positions: int = 5
    max_portfolio_exposure_pct: float = 80.0
    position_sizing_method: str = "half_kelly"
    use_isolated_margin: bool = True

    @field_validator("max_leverage")
    @classmethod
    def _cap_leverage(cls, v: float) -> float:
        if v > 10:
            raise ValueError("max_leverage must not exceed 10")
        return v

    @field_validator("position_sizing_method")
    @classmethod
    def _valid_sizing(cls, v: str) -> str:
        allowed = {"half_kelly", "quarter_kelly", "fixed_fractional"}
        if v not in allowed:
            raise ValueError(f"position_sizing_method must be one of {allowed}")
        return v


class TradingConfig(BaseModel):
    mode: str = "paper"
    base_currency: str = "USDT"
    trading_pairs: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    default_timeframe: str = "5m"
    min_profit_threshold_pct: float = 0.4
    maker_fee_pct: float = 0.1
    taker_fee_pct: float = 0.1
    estimated_slippage_pct: float = 0.05

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        allowed = {"paper", "live", "backtest"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}")
        return v


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    alert_on_trade: bool = True
    alert_on_error: bool = True
    alert_on_circuit_breaker: bool = True
    daily_summary: bool = True
    daily_summary_hour: int = 20


class DatabaseConfig(BaseModel):
    timescaledb_url: str = "postgresql+asyncpg://bot:password@localhost:5432/cryptoforge"
    redis_url: str = "redis://localhost:6379/0"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_output: bool = False
    log_dir: str = "logs"


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRYPTOFORGE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    strategies: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (mutates base)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(
    config_path: str | Path = "config/default.yaml",
    strategy_dir: str | Path = "config/strategies",
) -> AppConfig:
    """Load YAML config, merge strategy configs, apply env overrides."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    # merge strategy-specific YAMLs
    strat_dir = Path(strategy_dir)
    if strat_dir.is_dir():
        strategies: dict[str, Any] = {}
        for strat_file in strat_dir.glob("*.yaml"):
            with open(strat_file) as f:
                strat_data = yaml.safe_load(f) or {}
            strategies[strat_file.stem] = strat_data
        data.setdefault("strategies", {})
        _deep_merge(data["strategies"], strategies)

    # Remove empty strings so env vars can override them
    _strip_empty(data)

    # Load .env file if present
    from dotenv import load_dotenv
    load_dotenv()

    # Pydantic will also read env vars via CRYPTOFORGE_ prefix
    return AppConfig(**data)


def _strip_empty(d: dict) -> None:
    """Remove empty-string values so Pydantic env vars take precedence."""
    for key in list(d):
        val = d[key]
        if isinstance(val, dict):
            _strip_empty(val)
        elif val == "":
            del d[key]


def mask_secrets(cfg: AppConfig) -> dict[str, Any]:
    """Return config dict with secrets replaced by '***'."""
    d = cfg.model_dump()
    for section in d.values():
        if not isinstance(section, dict):
            continue
        for key in list(section):
            if any(s in key for s in ("key", "secret", "token", "password")):
                if section[key]:
                    section[key] = "***"
    return d
