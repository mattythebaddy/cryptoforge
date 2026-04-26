"""CLI script to backfill historical OHLCV data."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import click

from src.core.config import load_config
from src.core.logger import setup_logging
from src.data.historical_loader import HistoricalLoader
from src.data.models import init_db
from src.execution.exchange_client import ExchangeClient


@click.command()
@click.option("--symbol", default="BTC/USDT", help="Trading pair")
@click.option("--timeframe", default="1h", help="Candle timeframe")
@click.option("--days", default=365, help="Days of history to fetch")
@click.option("--config", default="config/default.yaml", help="Config file path")
def main(symbol: str, timeframe: str, days: int, config: str) -> None:
    asyncio.run(_run(symbol, timeframe, days, config))


async def _run(symbol: str, timeframe: str, days: int, config_path: str) -> None:
    cfg = load_config(config_path)
    setup_logging(level="INFO")

    await init_db(cfg.database.timescaledb_url)

    client = ExchangeClient(cfg.exchange)
    await client.connect()

    loader = HistoricalLoader(client.exchange)
    since = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - days * 86400,
        tz=timezone.utc,
    )
    count = await loader.backfill(symbol, timeframe, since)
    click.echo(f"Downloaded {count} candles for {symbol} {timeframe}")

    await client.close()


if __name__ == "__main__":
    main()
