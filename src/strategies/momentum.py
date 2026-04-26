"""Multi-indicator momentum / trend following strategy."""

from __future__ import annotations

from typing import Any

import pandas as pd
import structlog

from src.risk.risk_engine import Signal
from src.strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


class MomentumStrategy(BaseStrategy):
    """
    Catches trends using multi-indicator confirmation.
    Supports LONG and SHORT positions.
    Entry requires min_indicators_aligned confirmations. Rides with trailing stops.
    """

    def __init__(self, config: dict[str, Any], strategy_id: str = "momentum") -> None:
        super().__init__(config, strategy_id)
        self._min_aligned = int(config.get("min_indicators_aligned", 3))
        self._atr_stop_mult = float(config.get("atr_stop_multiplier", 2.0))
        self._trailing_atr_mult = float(config.get("trailing_stop_atr_mult", 2.5))
        self._rr_ratio = float(config.get("take_profit_rr_ratio", 3.0))
        self._cooldown_candles = int(config.get("cooldown_candles", 5))
        self._min_hold_candles = int(config.get("min_hold_candles", 5))
        self._candle_count = 0
        # Per-symbol state to prevent cross-contamination
        self._in_position: dict[str, bool] = {}
        self._side: dict[str, str] = {}  # "buy" (long) or "sell" (short)
        self._entry_price: dict[str, float] = {}
        self._entry_candle: dict[str, int] = {}
        self._highest_since_entry: dict[str, float] = {}
        self._lowest_since_entry: dict[str, float] = {}
        self._partial_taken: dict[str, bool] = {}  # Track partial profit exits

    async def on_candle(
        self, symbol: str, candle: dict[str, Any], indicators: pd.DataFrame
    ) -> Signal | None:
        if not self.is_active or len(indicators) < 50:
            return None

        configured_symbol = self.config.get("symbol", "")
        if configured_symbol and symbol != configured_symbol:
            return None

        self._candle_count += 1
        price = float(candle.get("close", 0))
        if price <= 0:
            return None

        latest = indicators.iloc[-1]
        atr = latest.get("atr_14", 0)
        if pd.isna(atr) or atr <= 0:
            return None

        rsi = latest.get("rsi_14", 50)
        if pd.isna(rsi):
            rsi = 50

        # --- EXIT LOGIC (if in position for THIS symbol) ---
        if self._in_position.get(symbol, False):
            side = self._side.get(symbol, "buy")

            # Minimum hold period — don't exit too early
            entry_candle = self._entry_candle.get(symbol, self._candle_count)
            candles_held = self._candle_count - entry_candle
            if candles_held < self._min_hold_candles:
                # Still update trailing stop trackers
                if side == "buy":
                    h = self._highest_since_entry.get(symbol, price)
                    if price > h:
                        self._highest_since_entry[symbol] = price
                else:
                    lo = self._lowest_since_entry.get(symbol, price)
                    if price < lo:
                        self._lowest_since_entry[symbol] = price
                return None

            # Calculate profit in R-multiples for adaptive trailing
            entry = self._entry_price.get(symbol, price)
            atr_risk = atr * self._atr_stop_mult
            if side == "buy":
                profit_r = (price - entry) / atr_risk if atr_risk > 0 else 0
            else:
                profit_r = (entry - price) / atr_risk if atr_risk > 0 else 0

            # Adaptive trailing: tighten as profit grows
            if profit_r >= 2.0:
                effective_trail = self._trailing_atr_mult * 0.5   # tight lock
            elif profit_r >= 1.0:
                effective_trail = self._trailing_atr_mult * 0.75  # moderate
            else:
                effective_trail = self._trailing_atr_mult          # default wide

            if side == "buy":
                # --- LONG EXIT ---
                highest = self._highest_since_entry.get(symbol, price)
                if price > highest:
                    self._highest_since_entry[symbol] = price
                    highest = price

                # Partial profit at +1R (take 50%)
                if profit_r >= 1.0 and not self._partial_taken.get(symbol, False):
                    self._partial_taken[symbol] = True
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="sell",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"Partial profit at +{profit_r:.1f}R (50% off)",
                    )

                # Adaptive trailing stop
                trailing_stop = highest - (atr * effective_trail)
                if price < trailing_stop:
                    self._in_position[symbol] = False
                    self._partial_taken.pop(symbol, None)
                    self._set_cooldown(self._candle_count, self._cooldown_candles)
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="sell",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"Adaptive trailing stop (trail={trailing_stop:.2f}, {effective_trail:.1f}x ATR)",
                    )

                # RSI overbought exit
                if rsi > 80:
                    self._in_position[symbol] = False
                    self._partial_taken.pop(symbol, None)
                    self._set_cooldown(self._candle_count, self._cooldown_candles)
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="sell",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"RSI overbought ({rsi:.1f})",
                    )

                # MACD declining for 3 candles
                if len(indicators) >= 4:
                    hist = indicators["macd_hist"].iloc[-3:].tolist()
                    if all(not pd.isna(h) for h in hist) and len(hist) == 3:
                        if hist[2] < hist[1] < hist[0]:
                            self._in_position[symbol] = False
                            self._partial_taken.pop(symbol, None)
                            self._set_cooldown(self._candle_count, self._cooldown_candles)
                            return Signal(
                                strategy_id=self.strategy_id,
                                symbol=symbol,
                                side="sell",
                                signal_type="exit",
                                price=price,
                                order_type="market",
                                reason="MACD declining 3 candles",
                            )

            else:
                # --- SHORT EXIT ---
                lowest = self._lowest_since_entry.get(symbol, price)
                if price < lowest:
                    self._lowest_since_entry[symbol] = price
                    lowest = price

                # Partial profit at +1R (take 50%)
                if profit_r >= 1.0 and not self._partial_taken.get(symbol, False):
                    self._partial_taken[symbol] = True
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="buy",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"Short partial profit at +{profit_r:.1f}R (50% off)",
                    )

                # Adaptive trailing stop (above)
                trailing_stop = lowest + (atr * effective_trail)
                if price > trailing_stop:
                    self._in_position[symbol] = False
                    self._partial_taken.pop(symbol, None)
                    self._set_cooldown(self._candle_count, self._cooldown_candles)
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="buy",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"Short adaptive trailing stop (trail={trailing_stop:.2f}, {effective_trail:.1f}x ATR)",
                    )

                # RSI oversold exit
                if rsi < 20:
                    self._in_position[symbol] = False
                    self._partial_taken.pop(symbol, None)
                    self._set_cooldown(self._candle_count, self._cooldown_candles)
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        side="buy",
                        signal_type="exit",
                        price=price,
                        order_type="market",
                        reason=f"RSI oversold ({rsi:.1f})",
                    )

                # MACD rising for 3 candles (cover short)
                if len(indicators) >= 4:
                    hist = indicators["macd_hist"].iloc[-3:].tolist()
                    if all(not pd.isna(h) for h in hist) and len(hist) == 3:
                        if hist[2] > hist[1] > hist[0]:
                            self._in_position[symbol] = False
                            self._partial_taken.pop(symbol, None)
                            self._set_cooldown(self._candle_count, self._cooldown_candles)
                            return Signal(
                                strategy_id=self.strategy_id,
                                symbol=symbol,
                                side="buy",
                                signal_type="exit",
                                price=price,
                                order_type="market",
                                reason="MACD rising 3 candles (short cover)",
                            )

            return None

        # --- ENTRY LOGIC ---
        if self._is_cooling_down(self._candle_count):
            return None

        # Shared indicators
        macd_hist = latest.get("macd_hist", 0)
        if pd.isna(macd_hist):
            macd_hist = 0
        prev_hist = 0
        if len(indicators) >= 2:
            prev_hist = indicators.iloc[-2].get("macd_hist", 0)
            if pd.isna(prev_hist):
                prev_hist = 0

        ema_21 = latest.get("ema_21", 0)
        ema_50 = latest.get("ema_50", 0)
        if pd.isna(ema_21):
            ema_21 = 0
        if pd.isna(ema_50):
            ema_50 = 0

        vwap = latest.get("vwap", 0)
        if pd.isna(vwap):
            vwap = 0

        vol_ratio = latest.get("volume_ratio", 1)
        if pd.isna(vol_ratio):
            vol_ratio = 1

        adx = latest.get("adx_14", 0)
        if pd.isna(adx):
            adx = 0

        bb_pct = latest.get("bb_pct", 0.5)
        if pd.isna(bb_pct):
            bb_pct = 0.5

        # ----- LONG confirmations -----
        long_confs = 0
        long_reasons: list[str] = []

        if 50 < rsi < 70:
            long_confs += 1
            long_reasons.append(f"RSI={rsi:.1f}")

        if macd_hist > 0 and macd_hist > prev_hist:
            long_confs += 1
            long_reasons.append("MACD+")

        if ema_21 > 0 and ema_50 > 0 and price > ema_21 > ema_50:
            long_confs += 1
            long_reasons.append("EMA aligned")

        if vwap > 0 and price > vwap:
            long_confs += 1
            long_reasons.append("Above VWAP")

        if vol_ratio > 1.5:
            long_confs += 1
            long_reasons.append(f"Vol {vol_ratio:.1f}x")

        if adx > 25:
            long_confs += 1
            long_reasons.append(f"ADX={adx:.1f}")

        if bb_pct > 0.5:
            long_confs += 1
            long_reasons.append(f"BB%={bb_pct:.2f}")

        # ----- SHORT confirmations -----
        short_confs = 0
        short_reasons: list[str] = []

        if 30 < rsi < 50:
            short_confs += 1
            short_reasons.append(f"RSI={rsi:.1f}")

        if macd_hist < 0 and macd_hist < prev_hist:
            short_confs += 1
            short_reasons.append("MACD-")

        if ema_21 > 0 and ema_50 > 0 and price < ema_21 < ema_50:
            short_confs += 1
            short_reasons.append("EMA bearish")

        if vwap > 0 and price < vwap:
            short_confs += 1
            short_reasons.append("Below VWAP")

        if vol_ratio > 1.5:
            short_confs += 1
            short_reasons.append(f"Vol {vol_ratio:.1f}x")

        if adx > 25:
            short_confs += 1
            short_reasons.append(f"ADX={adx:.1f}")

        if bb_pct < 0.5:
            short_confs += 1
            short_reasons.append(f"BB%={bb_pct:.2f}")

        # Take the stronger signal (long preferred on tie)
        if long_confs >= self._min_aligned and long_confs >= short_confs:
            stop_loss = price - (atr * self._atr_stop_mult)
            risk = price - stop_loss
            take_profit = price + (risk * self._rr_ratio)

            self._in_position[symbol] = True
            self._side[symbol] = "buy"
            self._entry_price[symbol] = price
            self._entry_candle[symbol] = self._candle_count
            self._highest_since_entry[symbol] = price
            self._partial_taken[symbol] = False

            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side="buy",
                signal_type="entry",
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence=min(long_confs / 7, 1.0),
                reason=f"LONG {long_confs} confirmations: {', '.join(long_reasons)}",
            )

        if short_confs >= self._min_aligned:
            stop_loss = price + (atr * self._atr_stop_mult)
            risk = stop_loss - price
            take_profit = price - (risk * self._rr_ratio)

            self._in_position[symbol] = True
            self._side[symbol] = "sell"
            self._entry_price[symbol] = price
            self._entry_candle[symbol] = self._candle_count
            self._lowest_since_entry[symbol] = price
            self._partial_taken[symbol] = False

            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side="sell",
                signal_type="entry",
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence=min(short_confs / 7, 1.0),
                reason=f"SHORT {short_confs} confirmations: {', '.join(short_reasons)}",
            )

        return None

    def get_required_indicators(self) -> list[str]:
        return [
            "rsi_14", "macd_hist", "ema_21", "ema_50", "vwap",
            "volume_ratio", "adx_14", "bb_pct", "atr_14",
        ]

    def get_required_timeframes(self) -> list[str]:
        return [self.config.get("timeframe", "1h")]

    def get_state(self) -> dict[str, Any]:
        base = super().get_state()
        base.update({
            "in_position": self._in_position,
            "side": self._side,
            "entry_price": self._entry_price,
            "entry_candle": self._entry_candle,
            "highest_since_entry": self._highest_since_entry,
            "lowest_since_entry": self._lowest_since_entry,
            "partial_taken": self._partial_taken,
            "candle_count": self._candle_count,
        })
        return base

    def load_state(self, state: dict[str, Any]) -> None:
        super().load_state(state)
        raw_pos = state.get("in_position", {})
        # Backwards-compatible: old state was bool, new state is dict
        if isinstance(raw_pos, bool):
            self._in_position = {}
        else:
            self._in_position = dict(raw_pos)
        self._side = state.get("side", {})
        if not isinstance(self._side, dict):
            self._side = {}
        raw_entry = state.get("entry_price", {})
        if isinstance(raw_entry, (int, float)):
            self._entry_price = {}
        else:
            self._entry_price = dict(raw_entry)
        raw_high = state.get("highest_since_entry", {})
        if isinstance(raw_high, (int, float)):
            self._highest_since_entry = {}
        else:
            self._highest_since_entry = dict(raw_high)
        raw_low = state.get("lowest_since_entry", {})
        if isinstance(raw_low, (int, float)):
            self._lowest_since_entry = {}
        else:
            self._lowest_since_entry = dict(raw_low)
        raw_ec = state.get("entry_candle", {})
        if isinstance(raw_ec, int):
            self._entry_candle = {}
        else:
            self._entry_candle = {k: int(v) for k, v in raw_ec.items()} if isinstance(raw_ec, dict) else {}
        raw_partial = state.get("partial_taken", {})
        if isinstance(raw_partial, bool):
            self._partial_taken = {}
        else:
            self._partial_taken = dict(raw_partial)
        self._candle_count = state.get("candle_count", 0)
