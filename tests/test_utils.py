"""Tests for utility modules."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.utils.math_utils import (
    fixed_fractional_size,
    half_kelly,
    max_drawdown,
    pct_change,
    round_to_precision,
    round_trip_cost_pct,
)
from src.utils.time_utils import (
    align_to_candle,
    dt_to_ts,
    next_candle_close,
    timeframe_to_seconds,
    ts_to_dt,
)


class TestTimeUtils:
    def test_ts_roundtrip(self) -> None:
        dt = datetime(2024, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        ts = dt_to_ts(dt)
        restored = ts_to_dt(ts)
        assert restored == dt

    def test_align_to_candle_5m(self) -> None:
        dt = datetime(2024, 1, 15, 12, 37, 42, tzinfo=timezone.utc)
        aligned = align_to_candle(dt, "5m")
        assert aligned.minute == 35
        assert aligned.second == 0

    def test_align_to_candle_1h(self) -> None:
        dt = datetime(2024, 1, 15, 12, 37, 42, tzinfo=timezone.utc)
        aligned = align_to_candle(dt, "1h")
        assert aligned.hour == 12
        assert aligned.minute == 0

    def test_next_candle_close(self) -> None:
        dt = datetime(2024, 1, 15, 12, 37, 42, tzinfo=timezone.utc)
        nxt = next_candle_close(dt, "5m")
        assert nxt.minute == 40
        assert nxt.second == 0

    def test_unknown_timeframe_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            timeframe_to_seconds("2w")


class TestMathUtils:
    def test_round_to_precision(self) -> None:
        assert round_to_precision(1.23456, 3) == 1.234  # floor
        assert round_to_precision(1.999, 2) == 1.99

    def test_pct_change(self) -> None:
        assert pct_change(100, 110) == pytest.approx(10.0)
        assert pct_change(100, 90) == pytest.approx(-10.0)
        assert pct_change(0, 10) == 0.0  # no division by zero

    def test_round_trip_cost(self) -> None:
        cost = round_trip_cost_pct(0.1, 0.1, 0.05)
        assert cost == pytest.approx(0.3)

    def test_half_kelly(self) -> None:
        # 55% win rate, 1.5 R/R
        k = half_kelly(0.55, 1.5, 1.0)
        assert 0 < k <= 0.05

    def test_half_kelly_zero_loss(self) -> None:
        assert half_kelly(0.5, 1.0, 0.0) == 0.0

    def test_fixed_fractional_size(self) -> None:
        # $10k equity, 1% risk, entry 65000, stop 63000
        size = fixed_fractional_size(10000, 1.0, 65000, 63000)
        assert size == pytest.approx(0.05)  # $100 risk / $2000 distance

    def test_max_drawdown(self) -> None:
        curve = [10000, 10500, 9500, 11000, 10000]
        dd = max_drawdown(curve)
        # peak was 10500, trough 9500 → ~9.5%
        assert dd == pytest.approx(9.523, abs=0.01)

    def test_max_drawdown_empty(self) -> None:
        assert max_drawdown([]) == 0.0
