"""Telegram alerts and command interface."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

import structlog

log = structlog.get_logger(__name__)


class TelegramAlertBot:
    """Sends alerts and accepts commands via Telegram."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._app: Any = None
        self._enabled = bool(bot_token and chat_id)
        # Command handlers registered externally
        self._command_handlers: dict[str, Callable[..., Coroutine]] = {}

    async def start(self) -> None:
        if not self._enabled:
            log.info("telegram.disabled", reason="no token/chat_id")
            return

        try:
            from telegram import Bot

            self._app = Bot(token=self._token)
            me = await self._app.get_me()
            log.info("telegram.started", bot=me.username)
        except Exception:
            log.exception("telegram.start_failed")
            self._enabled = False

    async def stop(self) -> None:
        self._app = None

    # -- Alerting --

    async def send_alert(self, message: str) -> None:
        if not self._enabled or not self._app:
            return
        try:
            try:
                await self._app.send_message(
                    chat_id=self._chat_id,
                    text=message[:4096],
                    parse_mode="Markdown",
                )
            except Exception:
                # Fallback: send without markdown if parsing fails
                await self._app.send_message(
                    chat_id=self._chat_id,
                    text=message[:4096],
                )
        except Exception:
            log.exception("telegram.send_failed")

    async def alert_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        strategy: str,
        pnl: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        emoji = "🟢" if side == "buy" else "🔴"
        action = "LONG ENTRY" if side == "buy" else "SHORT ENTRY"

        lines = [
            f"{emoji} *{symbol} {action}*",
            f"Strategy: `{strategy}`",
            f"Price: `${price:,.2f}`",
            f"Amount: `{amount:.6f}` (${amount * price:,.2f})",
        ]
        if stop_loss:
            sl_pct = (stop_loss - price) / price * 100
            lines.append(f"Stop Loss: `${stop_loss:,.2f}` ({sl_pct:+.1f}%)")
        if take_profit:
            tp_pct = (take_profit - price) / price * 100
            lines.append(f"Take Profit: `${take_profit:,.2f}` ({tp_pct:+.1f}%)")
        if pnl is not None:
            emoji_pnl = "💰" if pnl >= 0 else "💸"
            lines.append(f"{emoji_pnl} P&L: `${pnl:+,.2f}`")

        await self.send_alert("\n".join(lines))

    async def alert_position_closed(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        amount: float,
        pnl: float,
        strategy: str,
        close_reason: str = "",
    ) -> None:
        emoji = "💰" if pnl >= 0 else "💸"
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if side == "sell":
            pnl_pct = -pnl_pct
        notional = amount * entry_price
        result = "WIN" if pnl >= 0 else "LOSS"

        lines = [
            f"{emoji} *{symbol} CLOSED — {result}*",
            f"Strategy: {strategy}",
            f"Side: {side.upper()}",
            f"Entry: ${entry_price:,.2f} -> Exit: ${exit_price:,.2f} ({pnl_pct:+.1f}%)",
            f"Size: {amount:.6f} (${notional:,.2f})",
            f"P&L: ${pnl:+,.2f}",
        ]
        if close_reason:
            lines.append(f"Reason: {close_reason}")

        await self.send_alert("\n".join(lines))

    async def alert_signal_rejected(
        self,
        symbol: str,
        strategy: str,
        reason: str,
    ) -> None:
        """Optional: notify on rejected signals (disabled by default for noise)."""
        pass  # Enable if needed for debugging

    async def alert_circuit_breaker(self, breaker: str, details: str) -> None:
        await self.send_alert(f"⚡ *CIRCUIT BREAKER: {breaker}*\n{details}")

    async def alert_regime_change(
        self, old: str, new: str, strategies: list[str]
    ) -> None:
        pass  # Disabled: user only wants trade entry/exit notifications

    async def alert_error(self, module: str, error: str) -> None:
        await self.send_alert(f"⚠️ *Error in {module}*\n```\n{error[:500]}\n```")

    async def send_daily_summary(
        self,
        daily_pnl: float,
        trades: int,
        win_rate: float,
        best_trade: float,
        worst_trade: float,
        positions: list[dict[str, Any]],
        drawdown: float,
        strategies: list[str],
    ) -> None:
        emoji = "📈" if daily_pnl >= 0 else "📉"
        pos_lines = []
        for p in positions[:5]:
            pos_lines.append(f"  {p.get('symbol', '?')}: {p.get('side', '?')} ${p.get('pnl', 0):+,.2f}")

        msg = (
            f"{emoji} *Daily Summary*\n"
            f"P&L: `${daily_pnl:+,.2f}`\n"
            f"Trades: `{trades}` | Win Rate: `{win_rate:.0f}%`\n"
            f"Best: `${best_trade:+,.2f}` | Worst: `${worst_trade:+,.2f}`\n"
            f"Drawdown: `{drawdown:.1f}%`\n"
            f"Active: {', '.join(strategies)}\n"
        )
        if pos_lines:
            msg += f"*Positions:*\n" + "\n".join(pos_lines)

        await self.send_alert(msg)

    async def alert_health(self, score: int, details: str) -> None:
        emoji = "✅" if score >= 80 else "⚠️" if score >= 60 else "🔴"
        await self.send_alert(f"{emoji} *Health: {score}/100*\n{details}")
