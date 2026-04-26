"""Optimizer orchestrator — coordinates journal, analysis, tuning, allocation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from src.core.state_manager import StateManager
from src.monitoring.telegram_bot import TelegramAlertBot
from src.optimizer.capital_allocator import CapitalAllocator
from src.optimizer.param_optimizer import ParamAdjustment, ParameterOptimizer
from src.optimizer.performance_analyzer import PerformanceAnalyzer, StrategyMetrics
from src.optimizer.trade_journal import JournalEntry, TradeJournal
from src.strategies.base import BaseStrategy
from src.strategies.strategy_manager import StrategyManager

log = structlog.get_logger(__name__)


class OptimizerOrchestrator:
    """Wires together the self-improvement feedback loop."""

    def __init__(
        self,
        journal: TradeJournal,
        analyzer: PerformanceAnalyzer,
        param_optimizer: ParameterOptimizer,
        capital_allocator: CapitalAllocator,
        strategy_manager: StrategyManager,
        state_manager: StateManager,
        telegram: TelegramAlertBot,
        trigger_every_n_trades: int = 20,
    ) -> None:
        self._journal = journal
        self._analyzer = analyzer
        self._param_opt = param_optimizer
        self._cap_alloc = capital_allocator
        self._strat_mgr = strategy_manager
        self._state_mgr = state_manager
        self._telegram = telegram
        self._trigger_n = trigger_every_n_trades
        self._trade_counter = 0
        self._cycle_count = 0
        self._consecutive_losses = 0

    async def on_trade_completed(
        self,
        trade_result: dict[str, Any],
        entry_context: dict[str, Any],
        exit_regime: str,
        strategy: BaseStrategy | None,
    ) -> None:
        """Called after every paper trade exit. Journals + maybe optimizes."""
        # Build journal entry
        entry = JournalEntry(
            trade_id=0,
            symbol=trade_result.get("symbol", ""),
            strategy_id=trade_result.get("strategy", ""),
            side=trade_result.get("side", "buy"),
            entry_price=trade_result.get("entry_price", 0),
            exit_price=trade_result.get("exit_price", 0),
            amount=trade_result.get("amount", 0),
            pnl=trade_result.get("pnl", 0),
            pnl_pct=trade_result.get("pnl_pct", 0),
            result=trade_result.get("result", "loss"),
            hold_duration_candles=entry_context.get("hold_candles", 0),
            entry_reason=entry_context.get("entry_reason", ""),
            exit_reason=trade_result.get("reason", ""),
            entry_indicators=entry_context.get("indicators", {}),
            entry_regime=entry_context.get("regime", "unknown"),
            exit_regime=exit_regime,
            strategy_params=entry_context.get("strategy_params", {}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._journal.record(entry)
        self._trade_counter += 1

        # Track consecutive losses for emergency trigger
        if trade_result.get("result") == "loss":
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Emergency optimization on 4 consecutive losses
        emergency = (
            self._consecutive_losses >= 4
            and self._journal.total_count >= 5
        )
        if emergency:
            log.warning(
                "optimizer.emergency_trigger",
                consecutive_losses=self._consecutive_losses,
            )
            self._trade_counter = 0
            self._consecutive_losses = 0
            await self.run_optimization_cycle()
            return

        # Regular optimization cycle every N trades
        if (
            self._trade_counter >= self._trigger_n
            and self._journal.total_count >= self._trigger_n
        ):
            self._trade_counter = 0
            await self.run_optimization_cycle()

    async def run_optimization_cycle(self) -> None:
        """Full cycle: analyze -> adjust params -> rebalance capital -> notify."""
        self._cycle_count += 1
        log.info(
            "optimizer.cycle_start",
            cycle=self._cycle_count,
            total_trades=self._journal.total_count,
        )

        # 1. Analyze
        metrics = self._analyzer.compute_all_metrics(last_n=50)

        # 2. Optimize parameters
        all_param_changes: list[ParamAdjustment] = []
        for sid, strat in self._strat_mgr.strategies.items():
            adjustments = self._param_opt.optimize(sid, strat.config)
            if adjustments:
                self._param_opt.apply_adjustments(strat, adjustments)
                all_param_changes.extend(adjustments)

        # 3. Rebalance capital
        allocation_changes = self._cap_alloc.rebalance(metrics)

        # 4. Persist
        await self._persist_state()

        # 5. Notify (disabled — user only wants trade entry/exit notifications)

        log.info(
            "optimizer.cycle_complete",
            cycle=self._cycle_count,
            param_changes=len(all_param_changes),
            allocation_changes=len(allocation_changes),
        )

    async def _persist_state(self) -> None:
        try:
            await self._state_mgr.save_state(
                "optimizer_journal", self._journal.to_state()
            )
            await self._state_mgr.save_state(
                "optimizer_allocator", self._cap_alloc.to_state()
            )
        except Exception:
            log.exception("optimizer.persist_failed")

    async def _send_optimization_summary(
        self,
        metrics: dict[str, StrategyMetrics],
        param_changes: list[ParamAdjustment],
        allocation_changes: list,
    ) -> None:
        lines = [
            f"🧠 *Optimizer Cycle #{self._cycle_count}* ({self._journal.total_count} trades)\n",
            "*Strategy Performance:*",
            self._analyzer.format_summary(metrics),
        ]

        if param_changes:
            lines.append("\n*Parameter Adjustments:*")
            for adj in param_changes:
                lines.append(
                    f"  {adj.strategy_id}.{adj.param_name}: "
                    f"{adj.old_value:.3f} -> {adj.new_value:.3f}"
                )
        else:
            lines.append("\n_No parameter changes needed_")

        if allocation_changes:
            lines.append("\n*Capital Allocation:*")
            for ac in allocation_changes:
                lines.append(
                    f"  {ac.strategy_id}: {ac.old_multiplier:.2f}x -> "
                    f"{ac.new_multiplier:.2f}x ({ac.reason})"
                )

        # Top indicator correlations
        for sid in metrics:
            corrs = self._analyzer.compute_indicator_correlations(sid, 50)
            if corrs:
                lines.append(f"\n*{sid} winning indicators:*")
                for c in corrs[:3]:
                    lines.append(
                        f"  {c.indicator_name}: {c.direction} (score={c.score})"
                    )

        await self._telegram.send_alert("\n".join(lines))

    async def load_state(self) -> None:
        """Restore journal + allocator from state manager."""
        try:
            j_state = await self._state_mgr.load_state("optimizer_journal")
            if j_state:
                self._journal.from_state(j_state)

            a_state = await self._state_mgr.load_state("optimizer_allocator")
            if a_state:
                self._cap_alloc.from_state(a_state)
        except Exception:
            log.exception("optimizer.load_state_failed")
