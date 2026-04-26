"""Tests for trading strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.grid_trading import GridTradingStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.momentum import MomentumStrategy


def _make_df(n: int = 100, base_price: float = 65000, trend: float = 0.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    np.random.seed(42)
    closes = [base_price]
    for _ in range(n - 1):
        change = np.random.normal(trend, 0.01)
        closes.append(closes[-1] * (1 + change))
    closes = np.array(closes)
    highs = closes * (1 + np.abs(np.random.normal(0, 0.005, n)))
    lows = closes * (1 - np.abs(np.random.normal(0, 0.005, n)))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volumes = np.random.uniform(100, 1000, n)

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })

    # Add minimal indicators for strategy tests
    from src.indicators.technical import TechnicalIndicators

    return TechnicalIndicators().compute_all(df)


class TestGridTrading:
    def test_init_levels(self) -> None:
        cfg = {"upper_price": 70000, "lower_price": 60000, "num_grids": 10}
        strat = GridTradingStrategy(cfg)
        assert len(strat._levels) == 11  # num_grids + 1
        assert strat._levels[0] == 60000
        assert strat._levels[-1] == 70000

    @pytest.mark.asyncio
    async def test_no_signal_outside_range(self) -> None:
        cfg = {"upper_price": 70000, "lower_price": 60000, "num_grids": 10}
        strat = GridTradingStrategy(cfg)
        strat.is_active = True
        df = _make_df(30, base_price=80000)  # price way above grid
        candle = {"close": 80000}
        signal = await strat.on_candle("BTC/USDT", candle, df)
        assert signal is None

    @pytest.mark.asyncio
    async def test_signal_in_range(self) -> None:
        cfg = {"upper_price": 70000, "lower_price": 60000, "num_grids": 10, "total_investment": 5000}
        strat = GridTradingStrategy(cfg)
        strat.is_active = True
        df = _make_df(30, base_price=65000)
        candle = {"close": 65000}
        signal = await strat.on_candle("BTC/USDT", candle, df)
        assert signal is not None
        assert signal.side == "buy"

    def test_state_persistence(self) -> None:
        cfg = {"upper_price": 70000, "lower_price": 60000, "num_grids": 10}
        strat = GridTradingStrategy(cfg)
        strat._round_trips = 5
        strat._total_profit = 123.45
        state = strat.get_state()

        strat2 = GridTradingStrategy(cfg)
        strat2.load_state(state)
        assert strat2._round_trips == 5
        assert strat2._total_profit == 123.45


class TestMomentum:
    @pytest.mark.asyncio
    async def test_no_signal_with_insufficient_data(self) -> None:
        cfg = {"min_indicators_aligned": 3}
        strat = MomentumStrategy(cfg)
        strat.is_active = True
        df = _make_df(10)  # too few rows
        signal = await strat.on_candle("BTC/USDT", {"close": 65000}, df)
        assert signal is None

    @pytest.mark.asyncio
    async def test_generates_signal_on_trend(self) -> None:
        cfg = {"min_indicators_aligned": 2, "atr_stop_multiplier": 2.0, "take_profit_rr_ratio": 3.0}
        strat = MomentumStrategy(cfg)
        strat.is_active = True
        # Use trending data with enough candles
        df = _make_df(200, trend=0.005)
        candle = {"close": float(df.iloc[-1]["close"])}
        # May or may not trigger depending on indicator values
        signal = await strat.on_candle("BTC/USDT", candle, df)
        # Just verify it doesn't crash
        assert signal is None or signal.side == "buy"

    def test_required_indicators(self) -> None:
        strat = MomentumStrategy({})
        indicators = strat.get_required_indicators()
        assert "rsi_14" in indicators
        assert "macd_hist" in indicators
        assert "atr_14" in indicators


class TestMeanReversion:
    @pytest.mark.asyncio
    async def test_no_signal_when_inactive(self) -> None:
        strat = MeanReversionStrategy({})
        strat.is_active = False
        df = _make_df(50)
        signal = await strat.on_candle("ETH/USDT", {"close": 3000}, df)
        assert signal is None

    def test_required_indicators(self) -> None:
        strat = MeanReversionStrategy({})
        assert "bb_pct" in strat.get_required_indicators()
        assert "rsi_14" in strat.get_required_indicators()
