"""Tests for order manager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.execution.slippage_model import SlippageModel


class TestSlippageModel:
    def test_estimate_btc(self) -> None:
        model = SlippageModel()
        slip = model.estimate_slippage("BTC/USDT", "buy", 500)
        assert 0 < slip <= 1.0

    def test_estimate_unknown_symbol(self) -> None:
        model = SlippageModel()
        slip = model.estimate_slippage("SHIB/USDT", "buy", 500)
        assert slip >= 0.10  # mid-cap default

    def test_large_order_higher_slippage(self) -> None:
        model = SlippageModel()
        small = model.estimate_slippage("BTC/USDT", "buy", 100)
        large = model.estimate_slippage("BTC/USDT", "buy", 50000)
        assert large >= small

    def test_from_order_book(self) -> None:
        model = SlippageModel()
        book = {
            "bids": [[64990, 1.0], [64980, 2.0]],
            "asks": [[65010, 1.0], [65020, 2.0], [65050, 5.0]],
        }
        slip = model.estimate_slippage("BTC/USDT", "buy", 100000, order_book=book)
        assert slip >= 0

    def test_volatility_factor(self) -> None:
        model = SlippageModel()
        normal = model.estimate_slippage("BTC/USDT", "buy", 1000, current_atr_pct=1.0, avg_atr_pct=1.0)
        volatile = model.estimate_slippage("BTC/USDT", "buy", 1000, current_atr_pct=3.0, avg_atr_pct=1.0)
        assert volatile > normal
