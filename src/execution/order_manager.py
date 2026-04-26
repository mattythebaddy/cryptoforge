"""Order lifecycle management: create -> monitor -> fill/cancel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from src.core.event_bus import EventBus, EventType, make_event
from src.core.state_manager import StateManager
from src.execution.exchange_client import ExchangeClient
from src.risk.risk_engine import RiskDecision, Signal

log = structlog.get_logger(__name__)

_ORDER_TIMEOUT_S = 300  # 5 minutes for limit orders


@dataclass
class OrderGroup:
    """Linked orders: entry + stop_loss + take_profit."""

    strategy_id: str
    symbol: str
    side: str
    entry_order_id: str | None = None
    stop_loss_order_id: str | None = None
    take_profit_order_id: str | None = None
    entry_price: float = 0.0
    amount: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    status: str = "pending"  # pending, active, filled, cancelled
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class OrderManager:
    """Manages order lifecycle: creation -> monitoring -> fill/cancel."""

    def __init__(
        self,
        exchange: ExchangeClient,
        event_bus: EventBus,
        state_manager: StateManager,
        telegram: Any = None,
    ) -> None:
        self._exchange = exchange
        self._event_bus = event_bus
        self._state = state_manager
        self._telegram = telegram
        self._order_groups: dict[str, OrderGroup] = {}
        self._monitor_task: asyncio.Task[None] | None = None

    async def start_monitoring(self) -> None:
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="order_monitor"
        )

    async def stop_monitoring(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def submit_signal(
        self, signal: Signal, decision: RiskDecision
    ) -> str | None:
        """Convert approved signal to exchange orders."""
        try:
            # Place primary order
            order = await self._exchange.place_order(
                symbol=signal.symbol,
                order_type=signal.order_type,
                side=signal.side,
                amount=decision.adjusted_amount,
                price=signal.price if signal.order_type == "limit" else None,
            )

            order_id = order.get("id", "")
            group = OrderGroup(
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                side=signal.side,
                entry_order_id=order_id,
                entry_price=signal.price,
                amount=decision.adjusted_amount,
                stop_loss=decision.adjusted_stop_loss,
                take_profit=decision.adjusted_take_profit,
                status="pending",
                created_at=asyncio.get_event_loop().time(),
                metadata=signal.metadata or {},
            )

            self._order_groups[order_id] = group

            await self._event_bus.publish(
                make_event(
                    EventType.ORDER_PLACED,
                    "order_manager",
                    {
                        "order_id": order_id,
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "amount": decision.adjusted_amount,
                        "price": signal.price,
                        "strategy": signal.strategy_id,
                    },
                )
            )

            # Save state
            await self._save_state()
            return order_id

        except Exception:
            log.exception("order_manager.submit_failed", symbol=signal.symbol)
            await self._event_bus.publish(
                make_event(
                    EventType.ORDER_FAILED,
                    "order_manager",
                    {"symbol": signal.symbol, "strategy": signal.strategy_id},
                )
            )
            return None

    async def _monitor_loop(self) -> None:
        """Check order status every 5 seconds."""
        while True:
            try:
                await asyncio.sleep(5)
                await self._check_orders()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("order_monitor.error")
                await asyncio.sleep(10)

    async def _check_orders(self) -> None:
        now = asyncio.get_event_loop().time()

        for order_id, group in list(self._order_groups.items()):
            if group.status not in ("pending", "active"):
                continue

            try:
                # Query exchange
                order = await self._exchange.exchange.fetch_order(order_id, group.symbol)
                status = order.get("status", "")

                if status == "closed":
                    # Filled
                    fill_price = float(order.get("average", order.get("price", group.entry_price)))
                    group.status = "filled"

                    if group.stop_loss or group.take_profit:
                        await self._place_exit_orders(group, fill_price)

                    await self._event_bus.publish(
                        make_event(
                            EventType.ORDER_FILLED,
                            "order_manager",
                            {
                                "order_id": order_id,
                                "symbol": group.symbol,
                                "side": group.side,
                                "amount": group.amount,
                                "price": fill_price,
                                "strategy": group.strategy_id,
                            },
                        )
                    )

                    # Telegram: notify on fill
                    if self._telegram:
                        # Check if this is an exit order (SL/TP)
                        is_exit = (
                            order_id == group.stop_loss_order_id
                            or order_id == group.take_profit_order_id
                        )
                        if is_exit and group.entry_price > 0:
                            pnl = (fill_price - group.entry_price) * group.amount
                            if group.side == "sell":
                                pnl = -pnl
                            reason = "Stop Loss" if order_id == group.stop_loss_order_id else "Take Profit"
                            await self._telegram.alert_position_closed(
                                symbol=group.symbol,
                                side=group.side,
                                entry_price=group.entry_price,
                                exit_price=fill_price,
                                amount=group.amount,
                                pnl=pnl,
                                strategy=group.strategy_id,
                                close_reason=reason,
                            )

                    await self._save_state()

                elif status == "canceled" or status == "expired":
                    group.status = "cancelled"
                    await self._event_bus.publish(
                        make_event(
                            EventType.ORDER_CANCELLED,
                            "order_manager",
                            {"order_id": order_id, "symbol": group.symbol},
                        )
                    )

                elif group.status == "pending" and now - group.created_at > _ORDER_TIMEOUT_S:
                    # Timeout — cancel and retry as market
                    log.warning("order.timeout", order_id=order_id)
                    await self._exchange.cancel_order(order_id, group.symbol)
                    group.status = "cancelled"
                    # Retry as market order
                    await self._exchange.place_order(
                        group.symbol, "market", group.side, group.amount
                    )

            except Exception:
                log.exception("order_check.error", order_id=order_id)

    async def _place_exit_orders(self, group: OrderGroup, entry_price: float) -> None:
        """Place stop-loss and take-profit orders after entry fills."""
        exit_side = "sell" if group.side == "buy" else "buy"

        if group.stop_loss:
            try:
                sl_order = await self._exchange.place_order(
                    symbol=group.symbol,
                    order_type="limit",
                    side=exit_side,
                    amount=group.amount,
                    price=group.stop_loss,
                    params={"stopPrice": group.stop_loss, "type": "stop_market"},
                )
                group.stop_loss_order_id = sl_order.get("id")
            except Exception:
                log.exception("order.stop_loss_failed", symbol=group.symbol)

        if group.take_profit:
            try:
                tp_order = await self._exchange.place_order(
                    symbol=group.symbol,
                    order_type="limit",
                    side=exit_side,
                    amount=group.amount,
                    price=group.take_profit,
                )
                group.take_profit_order_id = tp_order.get("id")
            except Exception:
                log.exception("order.take_profit_failed", symbol=group.symbol)

    async def emergency_close_all(self) -> list[dict[str, Any]]:
        """Cancel ALL open orders and close ALL positions."""
        log.warning("order_manager.emergency_close")
        results = await self._exchange.cancel_all_orders()

        # Close positions
        positions = await self._exchange.get_positions()
        for pos in positions:
            if float(pos.get("contracts", 0)) > 0:
                side = "sell" if pos.get("side") == "long" else "buy"
                try:
                    r = await self._exchange.place_order(
                        pos["symbol"], "market", side, abs(float(pos["contracts"]))
                    )
                    results.append(r)
                except Exception:
                    log.exception("emergency_close.position_fail", symbol=pos.get("symbol"))

        self._order_groups.clear()
        await self._save_state()
        return results

    async def _save_state(self) -> None:
        state = {}
        for oid, g in self._order_groups.items():
            state[oid] = {
                "strategy_id": g.strategy_id,
                "symbol": g.symbol,
                "side": g.side,
                "amount": g.amount,
                "stop_loss": g.stop_loss,
                "take_profit": g.take_profit,
                "status": g.status,
            }
        await self._state.save_state("order_groups", state)

    async def load_state(self) -> None:
        state = await self._state.load_state("order_groups")
        if state:
            for oid, data in state.items():
                self._order_groups[oid] = OrderGroup(
                    strategy_id=data["strategy_id"],
                    symbol=data["symbol"],
                    side=data["side"],
                    entry_order_id=oid,
                    amount=data["amount"],
                    stop_loss=data.get("stop_loss"),
                    take_profit=data.get("take_profit"),
                    status=data["status"],
                )
