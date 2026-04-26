"""Backtesting engine with realistic fee and slippage modeling."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog

from src.indicators.technical import TechnicalIndicators
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)

# Startup candles excluded from trading
_WARMUP_CANDLES = 500


@dataclass
class BacktestResult:
    # Performance
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    buy_and_hold_return_pct: float = 0.0
    alpha: float = 0.0

    # Risk metrics
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    # Trade stats
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pnl_pct: float = 0.0
    avg_winner_pct: float = 0.0
    avg_loser_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    avg_holding_period: str = ""
    expectancy: float = 0.0

    # Fees
    total_fees_paid: float = 0.0
    fees_as_pct_of_profit: float = 0.0

    # Curves
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    monthly_returns: list[dict[str, Any]] = field(default_factory=list)
    trade_log: list[dict[str, Any]] = field(default_factory=list)


class BacktestEngine:
    """Event-driven backtesting with realistic execution modeling."""

    def __init__(
        self,
        maker_fee_pct: float = 0.1,
        taker_fee_pct: float = 0.1,
        slippage_pct: float = 0.05,
    ) -> None:
        self._maker_fee = maker_fee_pct / 100
        self._taker_fee = taker_fee_pct / 100
        self._slippage = slippage_pct / 100
        self._indicators = TechnicalIndicators()

    async def run(
        self,
        strategy: BaseStrategy,
        df: pd.DataFrame,
        initial_capital: float = 10000.0,
    ) -> BacktestResult:
        """
        Run backtest on OHLCV DataFrame.
        df must have columns: time, open, high, low, close, volume
        """
        if len(df) < _WARMUP_CANDLES + 50:
            log.warning("backtest.insufficient_data", rows=len(df))
            return BacktestResult()

        # Calculate indicators
        df = self._indicators.compute_all(df)

        # Simulate
        equity = initial_capital
        peak = equity
        position: dict[str, Any] | None = None
        trades: list[dict[str, Any]] = []
        equity_curve: list[dict[str, float]] = []
        total_fees = 0.0

        strategy.is_active = True

        for i in range(_WARMUP_CANDLES, len(df)):
            row = df.iloc[i]
            candle = {
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            indicators_slice = df.iloc[max(0, i - 200) : i + 1]

            price = row["close"]

            # Check stops if in position
            if position is not None:
                # Stop loss check (using low of candle)
                if position["stop_loss"] and row["low"] <= position["stop_loss"]:
                    exit_price = position["stop_loss"] * (1 - self._slippage)
                    fee = abs(position["size"]) * exit_price * self._taker_fee
                    total_fees += fee
                    pnl = (exit_price - position["entry"]) * position["size"] - fee - position["entry_fee"]
                    equity += position["size"] * exit_price - fee
                    trades.append({
                        "entry_time": position["entry_time"],
                        "exit_time": str(row.get("time", i)),
                        "entry": position["entry"],
                        "exit": exit_price,
                        "pnl": pnl,
                        "pnl_pct": pnl / (position["entry"] * abs(position["size"])) * 100,
                        "type": "stop_loss",
                        "holding": i - position["entry_idx"],
                    })
                    position = None
                    continue

                # Take profit check (using high of candle)
                if position.get("take_profit") and row["high"] >= position["take_profit"]:
                    exit_price = position["take_profit"] * (1 - self._slippage)
                    fee = abs(position["size"]) * exit_price * self._maker_fee
                    total_fees += fee
                    pnl = (exit_price - position["entry"]) * position["size"] - fee - position["entry_fee"]
                    equity += position["size"] * exit_price - fee
                    trades.append({
                        "entry_time": position["entry_time"],
                        "exit_time": str(row.get("time", i)),
                        "entry": position["entry"],
                        "exit": exit_price,
                        "pnl": pnl,
                        "pnl_pct": pnl / (position["entry"] * abs(position["size"])) * 100,
                        "type": "take_profit",
                        "holding": i - position["entry_idx"],
                    })
                    position = None
                    continue

            # Strategy signal
            signal = await strategy.on_candle("BACKTEST", candle, indicators_slice)

            if signal and signal.signal_type == "entry" and position is None:
                entry_price = price * (1 + self._slippage)
                size_usd = min(equity * 0.05, equity * 0.95)  # max 5%, leave buffer
                if signal.amount:
                    size_usd = min(signal.amount * entry_price, size_usd)
                size = size_usd / entry_price
                fee = size * entry_price * self._taker_fee
                total_fees += fee
                equity -= size * entry_price + fee
                position = {
                    "entry": entry_price,
                    "size": size,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "entry_time": str(row.get("time", i)),
                    "entry_idx": i,
                    "entry_fee": fee,
                }

            elif signal and signal.signal_type == "exit" and position is not None:
                exit_price = price * (1 - self._slippage)
                fee = abs(position["size"]) * exit_price * self._taker_fee
                total_fees += fee
                pnl = (exit_price - position["entry"]) * position["size"] - fee - position["entry_fee"]
                equity += position["size"] * exit_price - fee
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": str(row.get("time", i)),
                    "entry": position["entry"],
                    "exit": exit_price,
                    "pnl": pnl,
                    "pnl_pct": pnl / (position["entry"] * abs(position["size"])) * 100,
                    "type": "signal_exit",
                    "holding": i - position["entry_idx"],
                })
                position = None

            # Track equity
            mark_equity = equity
            if position:
                mark_equity += position["size"] * price
            if mark_equity > peak:
                peak = mark_equity
            dd = (peak - mark_equity) / peak * 100 if peak > 0 else 0
            equity_curve.append({"equity": mark_equity, "drawdown": dd})

        # Close any remaining position
        if position is not None:
            final_price = df.iloc[-1]["close"] * (1 - self._slippage)
            fee = abs(position["size"]) * final_price * self._taker_fee
            total_fees += fee
            pnl = (final_price - position["entry"]) * position["size"] - fee - position["entry_fee"]
            equity += position["size"] * final_price - fee
            trades.append({
                "entry": position["entry"],
                "exit": final_price,
                "pnl": pnl,
                "pnl_pct": pnl / (position["entry"] * abs(position["size"])) * 100,
                "type": "force_close",
                "holding": len(df) - position["entry_idx"],
            })

        return self._compute_result(
            trades, equity_curve, initial_capital, equity, total_fees, df
        )

    def _compute_result(
        self,
        trades: list[dict],
        equity_curve: list[dict],
        initial: float,
        final: float,
        fees: float,
        df: pd.DataFrame,
    ) -> BacktestResult:
        r = BacktestResult()
        r.trade_log = trades
        r.equity_curve = equity_curve
        r.total_fees_paid = fees

        # Returns
        r.total_return_pct = ((final - initial) / initial) * 100
        bnh_start = df.iloc[_WARMUP_CANDLES]["close"]
        bnh_end = df.iloc[-1]["close"]
        r.buy_and_hold_return_pct = ((bnh_end - bnh_start) / bnh_start) * 100
        r.alpha = r.total_return_pct - r.buy_and_hold_return_pct

        # Drawdown
        equities = [e["equity"] for e in equity_curve]
        if equities:
            from src.utils.math_utils import max_drawdown

            r.max_drawdown_pct = max_drawdown(equities)

        # Trade stats
        r.total_trades = len(trades)
        if trades:
            pnls = [t["pnl"] for t in trades]
            pnl_pcts = [t["pnl_pct"] for t in trades]
            winners = [p for p in pnls if p > 0]
            losers = [p for p in pnls if p <= 0]

            r.win_rate = len(winners) / len(trades) * 100
            gross_profit = sum(winners) if winners else 0
            gross_loss = abs(sum(losers)) if losers else 1
            r.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            r.avg_trade_pnl_pct = sum(pnl_pcts) / len(pnl_pcts)
            r.avg_winner_pct = sum(p for p in pnl_pcts if p > 0) / len(winners) if winners else 0
            r.avg_loser_pct = sum(p for p in pnl_pcts if p <= 0) / len(losers) if losers else 0
            r.best_trade_pct = max(pnl_pcts)
            r.worst_trade_pct = min(pnl_pcts)

            holdings = [t.get("holding", 0) for t in trades]
            r.avg_holding_period = f"{sum(holdings) / len(holdings):.1f} candles"

            w = r.win_rate / 100
            r.expectancy = (w * r.avg_winner_pct) + ((1 - w) * r.avg_loser_pct)

        # Sharpe / Sortino
        if len(equities) > 1:
            returns = np.diff(equities) / equities[:-1]
            if returns.std() > 0:
                r.sharpe_ratio = float(returns.mean() / returns.std() * math.sqrt(8760))
            neg = returns[returns < 0]
            if len(neg) > 0 and neg.std() > 0:
                r.sortino_ratio = float(returns.mean() / neg.std() * math.sqrt(8760))

        # Calmar
        if r.max_drawdown_pct > 0:
            r.calmar_ratio = r.total_return_pct / r.max_drawdown_pct

        # Fees
        profit = final - initial
        r.fees_as_pct_of_profit = (fees / profit * 100) if profit > 0 else 0

        return r
