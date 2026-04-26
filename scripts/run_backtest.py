"""CLI backtest runner."""

from __future__ import annotations

import asyncio

import click
import pandas as pd

from src.backtesting.engine import BacktestEngine
from src.backtesting.monte_carlo import MonteCarloSimulator
from src.backtesting.report import format_backtest_report, format_monte_carlo_report
from src.core.config import load_config
from src.core.logger import setup_logging
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.grid_trading import GridTradingStrategy


_STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "grid_trading": GridTradingStrategy,
}


@click.command()
@click.option("--strategy", type=click.Choice(list(_STRATEGIES.keys())), required=True)
@click.option("--data", required=True, help="Path to CSV with OHLCV data")
@click.option("--capital", default=10000.0, help="Initial capital")
@click.option("--config", default="config/default.yaml")
@click.option("--monte-carlo/--no-monte-carlo", default=True)
def main(strategy: str, data: str, capital: float, config: str, monte_carlo: bool) -> None:
    asyncio.run(_run(strategy, data, capital, config, monte_carlo))


async def _run(
    strategy_name: str, data_path: str, capital: float, config_path: str, run_mc: bool
) -> None:
    cfg = load_config(config_path)
    setup_logging(level="INFO")

    df = pd.read_csv(data_path, parse_dates=["time"] if "time" in pd.read_csv(data_path, nrows=1).columns else False)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            click.echo(f"Missing column: {col}")
            return

    strat_cfg = cfg.strategies.get(strategy_name, {})
    strat = _STRATEGIES[strategy_name](strat_cfg, strategy_name)

    engine = BacktestEngine(
        maker_fee_pct=cfg.trading.maker_fee_pct,
        taker_fee_pct=cfg.trading.taker_fee_pct,
        slippage_pct=cfg.trading.estimated_slippage_pct,
    )

    result = await engine.run(strat, df, capital)
    click.echo(format_backtest_report(result, f"Backtest: {strategy_name}"))

    if run_mc and result.trade_log:
        pnls = [t["pnl"] for t in result.trade_log]
        mc = MonteCarloSimulator().run(pnls, capital)
        click.echo(format_monte_carlo_report(mc, capital))


if __name__ == "__main__":
    main()
