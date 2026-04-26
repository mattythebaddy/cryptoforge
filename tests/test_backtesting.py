"""Tests for backtesting engine and Monte Carlo."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import BacktestEngine
from src.backtesting.monte_carlo import MonteCarloSimulator
from src.backtesting.report import format_backtest_report, format_monte_carlo_report
from src.strategies.momentum import MomentumStrategy


def _make_ohlcv(n: int = 600, base: float = 65000) -> pd.DataFrame:
    np.random.seed(42)
    closes = [base]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + np.random.normal(0.0002, 0.01)))
    closes = np.array(closes)
    return pd.DataFrame({
        "open": np.roll(closes, 1),
        "high": closes * (1 + np.abs(np.random.normal(0, 0.005, n))),
        "low": closes * (1 - np.abs(np.random.normal(0, 0.005, n))),
        "close": closes,
        "volume": np.random.uniform(100, 1000, n),
    })


class TestBacktestEngine:
    @pytest.mark.asyncio
    async def test_run_with_no_trades(self) -> None:
        engine = BacktestEngine()
        strat = MomentumStrategy({"min_indicators_aligned": 99})  # impossible to trigger
        df = _make_ohlcv(600)
        result = await engine.run(strat, df, 10000)
        assert result.total_trades == 0

    @pytest.mark.asyncio
    async def test_run_produces_equity_curve(self) -> None:
        engine = BacktestEngine()
        strat = MomentumStrategy({"min_indicators_aligned": 2})
        df = _make_ohlcv(800)
        result = await engine.run(strat, df, 10000)
        assert len(result.equity_curve) > 0

    @pytest.mark.asyncio
    async def test_fees_deducted(self) -> None:
        engine = BacktestEngine(maker_fee_pct=0.1, taker_fee_pct=0.1, slippage_pct=0.05)
        strat = MomentumStrategy({"min_indicators_aligned": 2})
        df = _make_ohlcv(800)
        result = await engine.run(strat, df, 10000)
        # If any trades happened, fees should be > 0
        if result.total_trades > 0:
            assert result.total_fees_paid > 0

    @pytest.mark.asyncio
    async def test_insufficient_data(self) -> None:
        engine = BacktestEngine()
        strat = MomentumStrategy({})
        df = _make_ohlcv(100)  # too few candles
        result = await engine.run(strat, df, 10000)
        assert result.total_trades == 0


class TestMonteCarlo:
    def test_basic_simulation(self) -> None:
        pnls = [50, -30, 80, -20, 40, -10, 60, -40, 30, 20]
        mc = MonteCarloSimulator()
        result = mc.run(pnls, initial_capital=10000, num_simulations=100)
        assert result.simulations == 100
        assert result.p50_equity > 0
        assert 0 <= result.probability_of_profit <= 1
        assert 0 <= result.probability_of_ruin <= 1

    def test_all_winning_trades(self) -> None:
        pnls = [100, 200, 150, 80, 300]
        result = MonteCarloSimulator().run(pnls, 10000, 500)
        assert result.probability_of_profit > 0.95
        assert result.probability_of_ruin == 0.0

    def test_all_losing_trades(self) -> None:
        pnls = [-500, -400, -600, -300, -700]
        result = MonteCarloSimulator().run(pnls, 10000, 500)
        assert result.probability_of_profit < 0.05
        assert result.p50_equity < 10000

    def test_empty_trades(self) -> None:
        result = MonteCarloSimulator().run([], 10000)
        assert result.simulations == 0


class TestReports:
    @pytest.mark.asyncio
    async def test_format_backtest_report(self) -> None:
        engine = BacktestEngine()
        strat = MomentumStrategy({"min_indicators_aligned": 2})
        df = _make_ohlcv(800)
        result = await engine.run(strat, df, 10000)
        report = format_backtest_report(result)
        assert "PERFORMANCE" in report
        assert "RISK" in report

    def test_format_monte_carlo_report(self) -> None:
        pnls = [50, -30, 80, -20, 40]
        mc_result = MonteCarloSimulator().run(pnls, 10000, 100)
        report = format_monte_carlo_report(mc_result, 10000)
        assert "Monte Carlo" in report
        assert "P(profit)" in report
