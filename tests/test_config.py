"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import AppConfig, load_config, mask_secrets
from src.core.exceptions import ConfigError


class TestLoadConfig:
    def test_loads_valid_yaml(self, config_path: Path) -> None:
        cfg = load_config(config_path, strategy_dir="nonexistent")
        assert cfg.exchange.name == "binance"
        assert cfg.exchange.testnet is True
        assert cfg.trading.mode == "paper"
        assert "BTC/USDT" in cfg.trading.trading_pairs
        assert cfg.risk.max_risk_per_trade_pct == 1.0

    def test_raises_on_missing_file(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config("does_not_exist.yaml")

    def test_default_values(self, config_path: Path) -> None:
        cfg = load_config(config_path, strategy_dir="nonexistent")
        assert cfg.risk.max_leverage == 3.0
        assert cfg.risk.position_sizing_method == "half_kelly"
        assert cfg.risk.use_isolated_margin is True

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("trading:\n  mode: yolo\n")
        with pytest.raises(Exception):
            load_config(bad, strategy_dir="nonexistent")

    def test_invalid_leverage_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("risk:\n  max_leverage: 100\n")
        with pytest.raises(Exception):
            load_config(bad, strategy_dir="nonexistent")

    def test_strategy_merge(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text("exchange:\n  name: binance\n")
        strat_dir = tmp_path / "strategies"
        strat_dir.mkdir()
        (strat_dir / "grid.yaml").write_text("symbol: BTC/USDT\nnum_grids: 20\n")
        cfg = load_config(cfg_file, strategy_dir=strat_dir)
        assert "grid" in cfg.strategies
        assert cfg.strategies["grid"]["num_grids"] == 20


class TestMaskSecrets:
    def test_masks_api_key(self, config_path: Path) -> None:
        cfg = load_config(config_path, strategy_dir="nonexistent")
        cfg.exchange.api_key = "super_secret_key"
        masked = mask_secrets(cfg)
        assert masked["exchange"]["api_key"] == "***"

    def test_secrets_masked_when_present(self, config_path: Path) -> None:
        cfg = load_config(config_path, strategy_dir="nonexistent")
        cfg.exchange.api_key = "some_test_key"
        cfg.exchange.api_secret = "some_test_secret"
        masked = mask_secrets(cfg)
        assert masked["exchange"]["api_key"] == "***"
        assert masked["exchange"]["api_secret"] == "***"
