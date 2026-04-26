"""Bollinger Band mean reversion strategy for ranging markets."""

from __future__ import annotations

from typing import Any

import pandas as pd
import structlog

from src.risk.risk_engine import Signal
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Trades price returning to Bollinger Band midline in ranging markets.
    Supports LONG (buy lower band) and SHORT (sell upper band).
    ONLY active when regime is RANGING.
    """

    def __init__(self, config: dict[str, Any], strategy_id: str = "mean_reversion") -> None:
        super().__init__(config, strategy_id)
        self._entry_bb_pct = float(config.get("entry_bb_pct", 0.05))
        self._exit_bb_pct = float(config.get("exit_bb_pct", 0.50))
        self._stop_bb_pct = float(config.get("stop_loss_bb_pct", -0.10))
        self._require_rsi = config.get("require_rsi_confirmation", True)
        self._require_volume = config.get("require_volume_spike", True)
        self._max_hold = int(config.get("max_hold_candles", 48))

        # Per-symbol state to prevent cross-contamination
        self._in_position: dict[str, bool] = {}
        self._side: dict[str, str] = {}  # "buy" (long) or "sell" (short)
        self._entry_candle: dict[str, int] = {}
        self._candle_count: int = 0

    async def on_candle(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> Signal | None:
        if not self.is_active or len(indicators) < 25:
            return None

        configured_symbol = self.config.get("symbol", "")
        if configured_symbol and symbol != configured_symbol:
            return None

        self._candle_count += 1
        price = float(candle.get("close", 0))
        if price <= 0:
            return None

        latest = indicators.iloc[-1]
        bb_pct = latest.get("bb_pct")
        bb_upper = latest.get("bb_upper")
        bb_lower = latest.get("bb_lower")
        bb_mid = latest.get("bb_mid")

        if any(pd.isna(v) for v in [bb_pct, bb_upper, bb_lower, bb_mid]):
            return None

        # --- EXIT (for THIS symbol) ---
        if self._in_position.get(symbol, False):
            side = self._side.get(symbol, "buy")
            entry_candle = self._entry_candle.get(symbol, 0)
            candles_held = self._candle_count - entry_candle

            if side == "buy":
                # LONG EXIT: price returned to midline
                if bb_pct >= self._exit_bb_pct:
                    self._in_position[symbol] = False
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="sell",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"BB midline reached (BB%={bb_pct:.2f})",
                    )
            else:
                # SHORT EXIT: price returned to midline from above
                if bb_pct <= self._exit_bb_pct:
                    self._in_position[symbol] = False
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="buy",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"BB midline reached from above (BB%={bb_pct:.2f})",
                    )

            # Max hold time (applies to both sides)
            if candles_held >= self._max_hold:
                self._in_position[symbol] = False
                exit_side = "sell" if side == "buy" else "buy"
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    side=exit_side,
                    signal_type="exit",
                    price=price,
                    order_type="market",
                    reason=f"Max hold {self._max_hold} candles exceeded",
                )

            return None

        # --- ENTRY ---
        rsi = latest.get("rsi_14", 50)
        if pd.isna(rsi):
            rsi = 50
        vol_ratio = latest.get("volume_ratio", 1)
        if pd.isna(vol_ratio):
            vol_ratio = 1

        bb_width = bb_upper - bb_lower

        # --- LONG ENTRY (near lower band) — scoring system ---
        if bb_pct <= self._entry_bb_pct:
            score = 1  # BB% at lower band is always +1 (required)
            reasons = [f"BB%={bb_pct:.3f}"]

            if rsi < 30:
                score += 1
                reasons.append(f"RSI={rsi:.1f}")

            if vol_ratio >= 1.5:
                score += 1
                reasons.append(f"Vol {vol_ratio:.1f}x")

            # Need 2 of 3 confirmations (BB% + at least one of RSI/volume)
            if score >= 2:
                stop_loss = bb_lower + (bb_width * self._stop_bb_pct)

                self._in_position[symbol] = True
                self._side[symbol] = "buy"
                self._entry_candle[symbol] = self._candle_count

                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    side="buy",
                    signal_type="entry",
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=bb_mid,
                    confidence=min(0.4 + score * 0.15, 0.85),
                    reason=f"Mean reversion LONG ({score}/3): {', '.join(reasons)}",
                )

        # --- SHORT ENTRY (near upper band) — scoring system ---
        if bb_pct >= (1.0 - self._entry_bb_pct):
            score = 1  # BB% at upper band is always +1 (required)
            reasons = [f"BB%={bb_pct:.3f}"]

            if rsi > 70:
                score += 1
                reasons.append(f"RSI={rsi:.1f}")

            if vol_ratio >= 1.5:
                score += 1
                reasons.append(f"Vol {vol_ratio:.1f}x")

            # Need 2 of 3 confirmations
            if score >= 2:
                stop_loss = bb_upper - (bb_width * self._stop_bb_pct)

                self._in_position[symbol] = True
                self._side[symbol] = "sell"
                self._entry_candle[symbol] = self._candle_count

                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    side="sell",
                    signal_type="entry",
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=bb_mid,
                    confidence=min(0.4 + score * 0.15, 0.85),
                    reason=f"Mean reversion SHORT ({score}/3): {', '.join(reasons)}",
                )

        return None

    def get_required_indicators(self) -> list[str]:
        return ["bb_pct", "bb_upper", "bb_lower", "bb_mid", "rsi_14", "volume_ratio"]

    def get_required_timeframes(self) -> list[str]:
        return [self.config.get("timeframe", "15m")]

    def get_state(self) -> dict[str, Any]:
        base = super().get_state()
        base.update({
            "in_position": self._in_position,
            "side": self._side,
            "entry_candle": self._entry_candle,
            "candle_count": self._candle_count,
        })
        return base

    def load_state(self, state: dict[str, Any]) -> None:
        super().load_state(state)
        raw_pos = state.get("in_position", {})
        if isinstance(raw_pos, bool):
            self._in_position = {}
        else:
            self._in_position = dict(raw_pos)
        self._side = state.get("side", {})
        if not isinstance(self._side, dict):
            self._side = {}
        raw_entry = state.get("entry_candle", {})
        if isinstance(raw_entry, int):
            self._entry_candle = {}
        else:
            self._entry_candle = {k: int(v) for k, v in raw_entry.items()}
        self._candle_count = state.get("candle_count", 0)
