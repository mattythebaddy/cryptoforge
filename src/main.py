"""CryptoForge entry point — orchestrates startup, main loop, and shutdown."""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import redis.asyncio as aioredis
import structlog

from src.core.config import AppConfig, load_config, mask_secrets
from src.core.event_bus import Event, EventBus, EventType, make_event
from src.core.logger import setup_logging
from src.core.state_manager import StateManager
from src.data.models import init_db
from src.execution.exchange_client import ExchangeClient
from src.data.feed_handler import FeedHandler
from src.data.historical_loader import HistoricalLoader
from src.data.orderbook_manager import OrderBookManager
from src.indicators.technical import TechnicalIndicators
from src.indicators.regime import MarketRegime, RegimeDetector
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.portfolio_manager import PortfolioManager
from src.risk.position_sizer import PositionSizer
from src.risk.risk_engine import RiskEngine
from src.strategies.strategy_manager import StrategyManager
from src.execution.order_manager import OrderManager
from src.monitoring.telegram_bot import TelegramAlertBot
from src.monitoring.metrics import (
    start_metrics_server, CANDLES_PROCESSED, SIGNALS_GENERATED,
    SIGNALS_REJECTED, EQUITY_TOTAL, DAILY_PNL, TRADES_TOTAL,
    PNL_TOTAL, WIN_RATE_7D, CONSECUTIVE_LOSSES, POSITION_VALUE,
)
from src.monitoring.health_check import HealthChecker

log: structlog.stdlib.BoundLogger | None = None

# ---------------------------------------------------------------------------
# Global references for graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_event: asyncio.Event | None = None
_exchange_client: ExchangeClient | None = None
_event_bus: EventBus | None = None
_state_manager: StateManager | None = None
_feed_handler: FeedHandler | None = None
_telegram: TelegramAlertBot | None = None
_order_manager: OrderManager | None = None


# ---------------------------------------------------------------------------
# Paper trade tracker — tracks positions, P&L, metrics in paper mode
# ---------------------------------------------------------------------------

class PaperTradeTracker:
    """Tracks paper positions, calculates P&L, feeds Prometheus + Telegram."""

    def __init__(self, starting_equity: float = 10_000.0) -> None:
        self._positions: dict[tuple[str, str], dict[str, Any]] = {}
        self._entry_context: dict[tuple[str, str], dict[str, Any]] = {}
        self._equity = starting_equity
        self._starting_equity = starting_equity
        self._daily_pnl = 0.0
        self._trades: list[dict[str, Any]] = []
        self._consecutive_losses = 0
        self._candle_counter = 0
        EQUITY_TOTAL.set(self._equity)

    @property
    def equity(self) -> float:
        return self._equity

    def record_entry(
        self,
        symbol: str,
        strategy: str,
        side: str,
        price: float,
        amount: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        key = (symbol, strategy)
        self._positions[key] = {
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "entry_price": price,
            "amount": amount,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_time": asyncio.get_event_loop().time(),
        }
        notional = amount * price
        TRADES_TOTAL.labels(
            symbol=symbol, side=side, strategy=strategy, result="open",
        ).inc()
        POSITION_VALUE.labels(symbol=symbol).set(notional)

    def record_exit(
        self,
        symbol: str,
        strategy: str,
        exit_price: float,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Close a paper position and return trade result, or None if no position."""
        key = (symbol, strategy)
        pos = self._positions.pop(key, None)
        if pos is None:
            return None

        entry_price = pos["entry_price"]
        amount = pos["amount"]
        side = pos["side"]

        # Calculate P&L (long only for now)
        if side == "buy":
            pnl = amount * (exit_price - entry_price)
        else:
            pnl = amount * (entry_price - exit_price)

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if side == "sell":
            pnl_pct = -pnl_pct

        # Update equity
        self._equity += pnl
        self._daily_pnl += pnl

        # Track trade history
        result = "win" if pnl >= 0 else "loss"
        trade = {
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount": amount,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "result": result,
            "reason": reason,
        }
        self._trades.append(trade)

        # Consecutive losses tracking
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Update Prometheus
        TRADES_TOTAL.labels(
            symbol=symbol, side="sell", strategy=strategy, result=result,
        ).inc()
        PNL_TOTAL.labels(symbol=symbol, strategy=strategy).set(
            sum(t["pnl"] for t in self._trades if t["symbol"] == symbol and t["strategy"] == strategy)
        )
        EQUITY_TOTAL.set(self._equity)
        DAILY_PNL.set(self._daily_pnl)
        POSITION_VALUE.labels(symbol=symbol).set(0)
        CONSECUTIVE_LOSSES.set(self._consecutive_losses)

        # Win rate (last 50 trades)
        recent = self._trades[-50:]
        if recent:
            wins = sum(1 for t in recent if t["result"] == "win")
            WIN_RATE_7D.set(wins / len(recent) * 100)

        return trade

    def stash_entry_context(
        self,
        symbol: str,
        strategy: str,
        indicators: dict[str, float],
        regime: str,
        strategy_params: dict[str, Any],
        entry_reason: str = "",
    ) -> None:
        """Store indicator/regime snapshot at entry time for the optimizer."""
        key = (symbol, strategy)
        self._entry_context[key] = {
            "indicators": indicators,
            "regime": regime,
            "strategy_params": strategy_params,
            "entry_reason": entry_reason,
            "entry_candle": self._candle_counter,
        }

    def pop_entry_context(self, symbol: str, strategy: str) -> dict[str, Any]:
        key = (symbol, strategy)
        ctx = self._entry_context.pop(key, {})
        if ctx:
            ctx["hold_candles"] = self._candle_counter - ctx.get("entry_candle", 0)
        return ctx

    def increment_candle(self) -> None:
        self._candle_counter += 1

    def has_position(self, symbol: str, strategy: str) -> bool:
        return (symbol, strategy) in self._positions

    def get_open_positions(self) -> list[dict[str, Any]]:
        return list(self._positions.values())

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl


# ---------------------------------------------------------------------------
# Candle buffer — keeps last N candles per symbol in memory for indicators
# ---------------------------------------------------------------------------
_candle_buffers: dict[str, list[dict[str, Any]]] = {}
_BUFFER_SIZE = 500


def _append_candle(symbol: str, candle: dict[str, Any]) -> pd.DataFrame:
    """Append candle to buffer and return as DataFrame with indicators."""
    if symbol not in _candle_buffers:
        _candle_buffers[symbol] = []
    _candle_buffers[symbol].append(candle)
    if len(_candle_buffers[symbol]) > _BUFFER_SIZE:
        _candle_buffers[symbol] = _candle_buffers[symbol][-_BUFFER_SIZE:]

    df = pd.DataFrame(_candle_buffers[symbol])
    return df


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def startup(config: AppConfig) -> dict[str, Any]:
    """Initialize all subsystems in order. Returns a dict of components."""
    global log
    log = structlog.get_logger("main")
    log.info("startup.begin", mode=config.trading.mode)

    components: dict[str, Any] = {}

    # 1. Database (optional — paper mode works without it)
    log.info("startup.database")
    try:
        await init_db(config.database.timescaledb_url)
    except Exception:
        log.warning("startup.database_failed", msg="Database unavailable — running without persistence")

    # 2. Redis (shared connection for hot cache — optional for paper mode)
    redis = None
    try:
        redis = aioredis.from_url(config.database.redis_url, decode_responses=False)
        await redis.ping()
    except Exception:
        log.warning("startup.redis_failed", msg="Redis unavailable — running without cache")
        redis = None

    # 3. Event bus
    log.info("startup.event_bus")
    event_bus = EventBus(config.database.redis_url)
    try:
        await event_bus.connect()
        await event_bus.start()
    except Exception:
        log.warning("startup.event_bus_failed", msg="Event bus running in local-only mode")
    components["event_bus"] = event_bus

    # 4. State manager + crash recovery
    log.info("startup.state_manager")
    state_manager = StateManager(config.database.redis_url)
    try:
        await state_manager.connect()
    except Exception:
        log.warning("startup.state_manager_failed", msg="State manager running without persistence")
    components["state_manager"] = state_manager

    # 5. Exchange client
    log.info("startup.exchange")
    exchange_client = ExchangeClient(config.exchange)
    try:
        await exchange_client.connect()
        components["exchange_client"] = exchange_client

        # Reconcile state with exchange
        try:
            report = await state_manager.reconcile_with_exchange(exchange_client)
            log.info("startup.reconciled", summary=report.summary())
        except Exception:
            log.warning("startup.reconcile_failed")
    except Exception:
        log.warning("startup.exchange_failed", msg="Running without exchange connection")
        components["exchange_client"] = None

    # 6. Indicators
    components["indicators"] = TechnicalIndicators()

    # 7. Regime detector
    regime_detector = RegimeDetector(event_bus)
    components["regime_detector"] = regime_detector

    # 8. Risk engine
    cb = CircuitBreaker(
        max_daily_loss_pct=config.risk.max_daily_loss_pct,
        max_drawdown_pct=config.risk.max_drawdown_pct,
        max_consecutive_losses=config.risk.max_consecutive_losses,
        event_bus=event_bus,
    )
    # Load circuit breaker state if available
    try:
        cb_state = await state_manager.load_state("circuit_breaker")
        if cb_state:
            cb.load_state(cb_state)
            log.info("startup.circuit_breaker_restored")
    except Exception:
        pass

    portfolio = PortfolioManager(config.risk.max_portfolio_exposure_pct)
    sizer = PositionSizer()

    risk_engine = RiskEngine(
        risk_config=config.risk,
        trading_config=config.trading,
        circuit_breaker=cb,
        portfolio_manager=portfolio,
        position_sizer=sizer,
        event_bus=event_bus,
    )

    # Set initial equity
    if components.get("exchange_client"):
        try:
            equity = await exchange_client.get_total_equity(config.trading.base_currency)
            risk_engine.set_equity(equity)
            cb.set_initial_equity(equity)
            log.info("startup.equity", equity=round(equity, 2))
        except Exception:
            log.warning("startup.equity_failed", msg="Using default equity")
            risk_engine.set_equity(10000)
            cb.set_initial_equity(10000)
    else:
        risk_engine.set_equity(10000)
        cb.set_initial_equity(10000)

    components["circuit_breaker"] = cb
    components["portfolio"] = portfolio
    components["risk_engine"] = risk_engine

    # 9. Strategy manager
    strategy_manager = StrategyManager(config.strategies, event_bus)
    # Load strategy states
    try:
        strat_states = await state_manager.load_state("strategy_states")
        if strat_states:
            strategy_manager.load_all_states(strat_states)
            log.info("startup.strategies_restored")
    except Exception:
        pass

    components["strategy_manager"] = strategy_manager

    # 10. Order manager
    if components.get("exchange_client"):
        order_manager = OrderManager(exchange_client, event_bus, state_manager)
        try:
            await order_manager.load_state()
        except Exception:
            log.warning("startup.order_manager_load_failed")
        components["order_manager"] = order_manager
    else:
        components["order_manager"] = None

    # 11. Orderbook manager
    components["orderbook"] = OrderBookManager()

    # 12. Feed handler (WebSocket data)
    if components.get("exchange_client"):
        feed = FeedHandler(
            exchange=exchange_client.exchange,
            event_bus=event_bus,
            redis=redis,
            symbols=config.trading.trading_pairs,
            timeframes=list(set(["1m", "5m", "15m", config.trading.default_timeframe])),
        )
        components["feed_handler"] = feed
    else:
        components["feed_handler"] = None

    # 13. Telegram
    telegram = TelegramAlertBot(config.telegram.bot_token, config.telegram.chat_id)
    await telegram.start()
    components["telegram"] = telegram

    # 13b. Wire telegram into order manager for close notifications
    if components.get("order_manager"):
        components["order_manager"]._telegram = telegram

    # 14. Paper trade tracker
    paper_tracker = PaperTradeTracker(starting_equity=10_000.0)
    components["paper_tracker"] = paper_tracker

    # 14b. Self-improvement optimizer
    from src.optimizer.trade_journal import TradeJournal
    from src.optimizer.performance_analyzer import PerformanceAnalyzer
    from src.optimizer.param_optimizer import ParameterOptimizer
    from src.optimizer.capital_allocator import CapitalAllocator
    from src.optimizer.orchestrator import OptimizerOrchestrator

    journal = TradeJournal()
    perf_analyzer = PerformanceAnalyzer(journal)
    param_opt = ParameterOptimizer(perf_analyzer, journal)
    cap_alloc = CapitalAllocator(perf_analyzer)

    optimizer = OptimizerOrchestrator(
        journal=journal,
        analyzer=perf_analyzer,
        param_optimizer=param_opt,
        capital_allocator=cap_alloc,
        strategy_manager=strategy_manager,
        state_manager=state_manager,
        telegram=telegram,
        trigger_every_n_trades=10,
    )
    try:
        await optimizer.load_state()
    except Exception:
        log.warning("startup.optimizer_load_failed")
    components["optimizer"] = optimizer

    # Wire capital allocator into risk engine
    risk_engine.set_capital_allocator(cap_alloc)

    # 15. Health checker
    components["health_checker"] = HealthChecker(
        exchange_client=components.get("exchange_client"),
        event_bus=event_bus,
        circuit_breaker=cb,
        strategy_manager=strategy_manager,
    )

    # 15. Prometheus metrics
    try:
        start_metrics_server(9090)
    except Exception:
        log.warning("startup.metrics_failed", msg="Prometheus metrics port in use or unavailable")

    # 15b. Dashboard API server (port 8050)
    try:
        from src.api.server import create_api_app, start_api_server
        api_app = create_api_app(components)
        api_task = asyncio.create_task(start_api_server(api_app, port=8050))
        components["api_task"] = api_task
        log.info("startup.dashboard", port=8050, url="http://localhost:8050")
    except Exception:
        log.warning("startup.dashboard_failed", msg="Dashboard API could not start")

    # 16. Historical warmup — backfill AND pre-fill in-memory candle buffers
    if components.get("exchange_client"):
        log.info("startup.warmup", msg="Backfilling candles for indicator warmup...")
        try:
            loader = HistoricalLoader(exchange_client.exchange)
            for symbol in config.trading.trading_pairs:
                for tf in ["1m", "5m", "15m"]:
                    candles = await exchange_client.exchange.fetch_ohlcv(
                        symbol, tf, limit=200
                    )
                    if candles:
                        for c in candles:
                            _append_candle(symbol, {
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                                "volume": float(c[5]) if c[5] else 0.0,
                            })
                        log.info(
                            "startup.buffer_filled",
                            symbol=symbol,
                            timeframe=tf,
                            candles=len(candles),
                        )
            log.info("startup.warmup_complete")
        except Exception:
            log.exception("startup.warmup_failed")

    # Publish startup event
    try:
        await event_bus.publish(
            make_event(EventType.HEALTH_CHECK, "main", {"status": "started"})
        )
    except Exception:
        pass  # Event bus unavailable

    # Startup/shutdown/health alerts disabled — user only wants trade notifications

    log.info("startup.complete", trading_pairs=config.trading.trading_pairs)
    return components


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

async def main_loop(
    config: AppConfig,
    components: dict[str, Any],
    shutdown_event: asyncio.Event,
) -> None:
    """Full event-driven main loop."""
    mlog = structlog.get_logger("main_loop")

    event_bus: EventBus = components["event_bus"]
    state_manager: StateManager = components["state_manager"]
    indicators_calc: TechnicalIndicators = components["indicators"]
    regime_detector: RegimeDetector = components["regime_detector"]
    risk_engine: RiskEngine = components["risk_engine"]
    strategy_manager: StrategyManager = components["strategy_manager"]
    order_manager: OrderManager | None = components.get("order_manager")
    telegram: TelegramAlertBot = components["telegram"]
    health_checker: HealthChecker = components["health_checker"]
    feed_handler: FeedHandler | None = components.get("feed_handler")
    orderbook: OrderBookManager = components["orderbook"]
    paper_tracker: PaperTradeTracker = components["paper_tracker"]
    optimizer = components.get("optimizer")

    # Activate strategies for initial regime
    current_regime = regime_detector.current_regime
    strategy_manager.activate_for_regime(current_regime)
    mlog.info("main_loop.strategies_activated", regime=current_regime,
              active=[s.strategy_id for s in strategy_manager.get_active_strategies()])

    # --- Register event handlers ---

    async def on_candle_closed(event: Event) -> None:
        """Core trading logic: indicators → regime → strategies → risk → execute."""
        nonlocal current_regime

        data = event.data
        symbol = data.get("symbol", "")
        if not symbol:
            return

        CANDLES_PROCESSED.inc()
        paper_tracker.increment_candle()

        candle = {
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data["volume"],
        }

        # Build indicator DataFrame
        df = _append_candle(symbol, candle)
        if len(df) < 50:
            return  # not enough data yet

        df = indicators_calc.compute_all(df)
        health_checker.set_last_candle_time(asyncio.get_event_loop().time())

        # Regime detection
        new_regime = regime_detector.detect(df)
        if new_regime != current_regime:
            await strategy_manager.on_regime_change(current_regime, new_regime)
            current_regime = new_regime

        # Evaluate strategies
        signals = await strategy_manager.evaluate_all(symbol, candle, df)

        for sig in signals:
            SIGNALS_GENERATED.labels(strategy=sig.strategy_id).inc()
            mlog.info(
                "signal.generated",
                symbol=sig.symbol,
                side=sig.side,
                strategy=sig.strategy_id,
                reason=sig.reason,
            )

            # Session-aware filter: skip entries during low-liquidity hours
            skip_hours = config.trading.__dict__.get("skip_hours_utc", None)
            if skip_hours and sig.signal_type == "entry":
                hour_utc = datetime.now(timezone.utc).hour
                if hour_utc in skip_hours:
                    mlog.debug("signal.skipped_session", hour=hour_utc, strategy=sig.strategy_id)
                    continue

            # Risk engine evaluation
            decision = await risk_engine.evaluate_signal(sig)

            if decision.approved:
                is_paper = config.trading.mode == "paper"

                if order_manager and not is_paper:
                    # LIVE mode — send to exchange
                    order_id = await order_manager.submit_signal(sig, decision)
                    if order_id:
                        await telegram.alert_trade(
                            symbol=sig.symbol,
                            side=sig.side,
                            price=sig.price,
                            amount=decision.adjusted_amount,
                            strategy=sig.strategy_id,
                            stop_loss=decision.adjusted_stop_loss,
                            take_profit=decision.adjusted_take_profit,
                        )
                else:
                    # PAPER mode — full position tracking + notifications
                    notional = decision.adjusted_amount * sig.price
                    action = "ENTRY" if sig.signal_type == "entry" else "EXIT"

                    if sig.signal_type == "entry" and decision.adjusted_amount > 0:
                        # Record entry
                        paper_tracker.record_entry(
                            symbol=sig.symbol,
                            strategy=sig.strategy_id,
                            side=sig.side,
                            price=sig.price,
                            amount=decision.adjusted_amount,
                            stop_loss=decision.adjusted_stop_loss,
                            take_profit=decision.adjusted_take_profit,
                        )
                        mlog.info(
                            "paper.trade",
                            action="ENTRY",
                            symbol=sig.symbol,
                            side=sig.side,
                            amount=round(decision.adjusted_amount, 8),
                            price=sig.price,
                            notional=round(notional, 2),
                            strategy=sig.strategy_id,
                            reason=sig.reason,
                            equity=round(paper_tracker.equity, 2),
                        )
                        await telegram.alert_trade(
                            symbol=sig.symbol,
                            side=sig.side,
                            price=sig.price,
                            amount=decision.adjusted_amount,
                            strategy=sig.strategy_id,
                            stop_loss=decision.adjusted_stop_loss,
                            take_profit=decision.adjusted_take_profit,
                        )

                        # Push live update to dashboard WebSocket
                        try:
                            from src.api.server import broadcast_update
                            await broadcast_update({
                                "type": "trade_entry",
                                "symbol": sig.symbol,
                                "side": sig.side,
                                "price": sig.price,
                                "amount": decision.adjusted_amount,
                                "strategy": sig.strategy_id,
                                "equity": paper_tracker.equity,
                            })
                        except Exception:
                            pass  # Dashboard not running

                        # Stash entry context for optimizer
                        indicator_row = df.iloc[-1]
                        indicator_snapshot = {}
                        for col in ["rsi_14", "macd_hist", "bb_pct", "adx_14", "atr_14", "volume_ratio"]:
                            val = indicator_row.get(col)
                            if val is not None and not pd.isna(val):
                                indicator_snapshot[col] = float(val)
                        strat_obj = strategy_manager.strategies.get(sig.strategy_id)
                        paper_tracker.stash_entry_context(
                            symbol=sig.symbol,
                            strategy=sig.strategy_id,
                            indicators=indicator_snapshot,
                            regime=str(regime_detector.current_regime),
                            strategy_params=dict(strat_obj.config) if strat_obj else {},
                            entry_reason=sig.reason,
                        )

                    elif sig.signal_type == "exit":
                        # Record exit and calculate P&L
                        trade_result = paper_tracker.record_exit(
                            symbol=sig.symbol,
                            strategy=sig.strategy_id,
                            exit_price=sig.price,
                            reason=sig.reason,
                        )
                        if trade_result:
                            # Keep risk engine equity in sync with paper tracker
                            risk_engine.set_equity(paper_tracker.equity)

                            mlog.info(
                                "paper.trade",
                                action="EXIT",
                                symbol=sig.symbol,
                                side=sig.side,
                                entry=trade_result["entry_price"],
                                exit=trade_result["exit_price"],
                                pnl=round(trade_result["pnl"], 4),
                                pnl_pct=f"{trade_result['pnl_pct']:+.2f}%",
                                result=trade_result["result"],
                                strategy=sig.strategy_id,
                                reason=sig.reason,
                                equity=round(paper_tracker.equity, 2),
                                total_trades=paper_tracker.trade_count,
                            )
                            # Telegram close notification with P&L
                            await telegram.alert_position_closed(
                                symbol=sig.symbol,
                                side=trade_result["side"],
                                entry_price=trade_result["entry_price"],
                                exit_price=trade_result["exit_price"],
                                amount=trade_result["amount"],
                                pnl=trade_result["pnl"],
                                strategy=sig.strategy_id,
                                close_reason=sig.reason,
                            )
                            # Push live update to dashboard WebSocket
                            try:
                                from src.api.server import broadcast_update
                                await broadcast_update({
                                    "type": "trade_exit",
                                    "symbol": sig.symbol,
                                    "side": trade_result["side"],
                                    "entry_price": trade_result["entry_price"],
                                    "exit_price": trade_result["exit_price"],
                                    "pnl": trade_result["pnl"],
                                    "pnl_pct": trade_result["pnl_pct"],
                                    "result": trade_result["result"],
                                    "strategy": sig.strategy_id,
                                    "equity": paper_tracker.equity,
                                    "total_trades": paper_tracker.trade_count,
                                })
                            except Exception:
                                pass  # Dashboard not running

                            # Feed trade result to optimizer
                            if optimizer:
                                entry_ctx = paper_tracker.pop_entry_context(
                                    sig.symbol, sig.strategy_id,
                                )
                                strat_obj = strategy_manager.strategies.get(
                                    sig.strategy_id
                                )
                                await optimizer.on_trade_completed(
                                    trade_result=trade_result,
                                    entry_context=entry_ctx,
                                    exit_regime=str(regime_detector.current_regime),
                                    strategy=strat_obj,
                                )
                        else:
                            mlog.info(
                                "paper.trade",
                                action="EXIT_NO_POS",
                                symbol=sig.symbol,
                                strategy=sig.strategy_id,
                                reason=sig.reason,
                            )

                    # Notify strategy of paper fill (grid needs this)
                    strat = strategy_manager.strategies.get(sig.strategy_id)
                    if strat:
                        await strat.on_trade_update({
                            "side": sig.side,
                            "symbol": sig.symbol,
                            "price": sig.price,
                            "amount": decision.adjusted_amount,
                            "metadata": sig.metadata or {},
                            "realized_pnl": 0,
                        })
            else:
                SIGNALS_REJECTED.labels(
                    strategy=sig.strategy_id, reason=decision.rejection_reason[:50]
                ).inc()

    async def on_orderbook_update(event: Event) -> None:
        data = event.data
        orderbook.update(
            data.get("symbol", ""),
            data.get("bids", []),
            data.get("asks", []),
        )

    event_bus.subscribe(EventType.CANDLE_CLOSED, on_candle_closed)
    event_bus.subscribe(EventType.ORDERBOOK_UPDATE, on_orderbook_update)

    # --- Start data feed ---
    if feed_handler:
        await feed_handler.start()
        mlog.info("main_loop.feed_started")

    # --- Start order monitoring ---
    if order_manager:
        await order_manager.start_monitoring()

    mlog.info("main_loop.running", msg="Processing live market data")

    # --- Periodic tasks ---
    health_counter = 0
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
        except (asyncio.TimeoutError, TimeoutError):
            health_counter += 1

            # Health check every 4 minutes
            if health_counter % 4 == 0:
                await health_checker.check()

            # Save state every 2 minutes
            if health_counter % 2 == 0:
                try:
                    await state_manager.save_state(
                        "strategy_states", strategy_manager.get_all_states()
                    )
                    await state_manager.save_state(
                        "circuit_breaker", components["circuit_breaker"].get_state()
                    )
                except Exception:
                    pass  # State manager unavailable

            # Log heartbeat
            active = [s.strategy_id for s in strategy_manager.get_active_strategies()]
            candle_counts = {sym: len(buf) for sym, buf in _candle_buffers.items()}
            mlog.debug(
                "heartbeat",
                regime=regime_detector.current_regime,
                active_strategies=active,
                candles=candle_counts,
            )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def shutdown(components: dict[str, Any], shutdown_event: asyncio.Event) -> None:
    """Graceful shutdown sequence."""
    slog = structlog.get_logger("shutdown")
    slog.info("shutdown.begin")
    shutdown_event.set()

    telegram: TelegramAlertBot | None = components.get("telegram")
    event_bus: EventBus | None = components.get("event_bus")
    state_manager: StateManager | None = components.get("state_manager")
    feed_handler: FeedHandler | None = components.get("feed_handler")
    order_manager: OrderManager | None = components.get("order_manager")
    exchange_client: ExchangeClient | None = components.get("exchange_client")

    # 1. Stop feed
    if feed_handler:
        await feed_handler.stop()

    # 2. Stop order monitoring
    if order_manager:
        await order_manager.stop_monitoring()

    # 3. Publish shutdown event (may fail if Redis is down)
    if event_bus:
        try:
            await event_bus.publish(
                make_event(EventType.SHUTDOWN, "main", {"reason": "graceful"})
            )
        except Exception:
            pass  # Event bus unavailable

    # 4. Checkpoint all state
    if state_manager:
        try:
            strategy_manager = components.get("strategy_manager")
            if strategy_manager:
                await state_manager.save_state(
                    "strategy_states", strategy_manager.get_all_states()
                )
            cb = components.get("circuit_breaker")
            if cb:
                await state_manager.save_state("circuit_breaker", cb.get_state())
            opt = components.get("optimizer")
            if opt:
                await opt._persist_state()
            await state_manager.checkpoint()
            slog.info("shutdown.state_saved")
        except Exception:
            slog.exception("shutdown.state_save_failed")

    # 5. Close exchange
    if exchange_client:
        await exchange_client.close()

    # 6. Stop event bus
    if event_bus:
        await event_bus.stop()

    # 7. Close state manager
    if state_manager:
        await state_manager.close()

    # 8. Send telegram notification
    if telegram:
        await telegram.stop()

    slog.info("shutdown.complete")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run() -> None:
    """Top-level async runner."""
    config_path = Path("config/default.yaml")
    config = load_config(config_path)

    setup_logging(
        level=config.logging.level,
        json_output=config.logging.json_output,
        log_dir=config.logging.log_dir,
    )

    global log
    log = structlog.get_logger("main")
    log.info("config.loaded", config=mask_secrets(config))

    shutdown_event = asyncio.Event()
    components: dict[str, Any] = {}

    try:
        components = await startup(config)
    except Exception:
        log.exception("startup.failed")
        sys.exit(1)

    # Signal handlers (Unix)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda: asyncio.ensure_future(shutdown(components, shutdown_event)),
            )
        except NotImplementedError:
            pass  # Windows

    try:
        await main_loop(config, components, shutdown_event)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        if not shutdown_event.is_set():
            await shutdown(components, shutdown_event)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
