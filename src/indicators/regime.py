"""Market regime detection — the meta-strategy."""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import pandas as pd
import structlog

from src.core.event_bus import EventBus, EventType, make_event

log = structlog.get_logger(__name__)


class MarketRegime(StrEnum):
    STRONG_UPTREND = "strong_uptrend"
    WEAK_UPTREND = "weak_uptrend"
    RANGING = "ranging"
    WEAK_DOWNTREND = "weak_downtrend"
    STRONG_DOWNTREND = "strong_downtrend"
    HIGH_VOLATILITY = "high_volatility"
    CRASH = "crash"


# Which strategies should be active in each regime
STRATEGY_MAP: dict[MarketRegime, list[str]] = {
    MarketRegime.STRONG_UPTREND: ["momentum", "grid_trading", "mean_reversion", "dca_fear"],
    MarketRegime.WEAK_UPTREND: ["momentum", "grid_trading", "mean_reversion", "dca_fear"],
    MarketRegime.RANGING: ["mean_reversion", "grid_trading", "momentum", "dca_fear"],
    MarketRegime.WEAK_DOWNTREND: ["mean_reversion", "grid_trading", "dca_fear"],
    MarketRegime.STRONG_DOWNTREND: ["mean_reversion", "dca_fear"],
    MarketRegime.HIGH_VOLATILITY: ["momentum", "grid_trading", "dca_fear"],
    MarketRegime.CRASH: ["dca_fear"],
}


class RegimeDetector:
    """
    Combines multiple signals to classify current market regime.
    Requires 3 consecutive confirmations before switching.
    Supports multi-timeframe consensus for stronger regime validation.
    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self._event_bus = event_bus
        self._current_regime = MarketRegime.RANGING
        self._candidate: MarketRegime | None = None
        self._confirmation_count = 0
        self._required_confirmations = 3
        # Multi-timeframe regime tracking
        self._regime_by_tf: dict[str, MarketRegime] = {}

    @property
    def current_regime(self) -> MarketRegime:
        return self._current_regime

    @property
    def regime_by_timeframe(self) -> dict[str, MarketRegime]:
        return dict(self._regime_by_tf)

    def detect(self, df: pd.DataFrame, timeframe: str = "5m") -> MarketRegime:
        """Classify regime from indicator DataFrame. Returns the confirmed regime."""
        if len(df) < 50:
            return self._current_regime

        latest = df.iloc[-1]
        raw_regime = self._classify_raw(df, latest)

        # Store per-timeframe regime
        self._regime_by_tf[timeframe] = raw_regime

        # Apply multi-timeframe consensus if we have data from multiple TFs
        consensus = self._consensus_regime(raw_regime)

        # Confirmation logic — require N consecutive same classifications
        if consensus == self._candidate:
            self._confirmation_count += 1
        else:
            self._candidate = consensus
            self._confirmation_count = 1

        if (
            self._confirmation_count >= self._required_confirmations
            and consensus != self._current_regime
        ):
            old = self._current_regime
            self._current_regime = consensus
            log.info("regime.changed", old=old, new=consensus,
                     timeframe_regimes=dict(self._regime_by_tf))
            return consensus

        return self._current_regime

    def _consensus_regime(self, primary: MarketRegime) -> MarketRegime:
        """Require multi-TF agreement for trending/ranging. Falls back to primary."""
        if len(self._regime_by_tf) < 2:
            return primary  # Only one TF available, use raw

        tf_regimes = list(self._regime_by_tf.values())

        # CRASH and HIGH_VOLATILITY override everything (any TF)
        if any(r == MarketRegime.CRASH for r in tf_regimes):
            return MarketRegime.CRASH
        if any(r == MarketRegime.HIGH_VOLATILITY for r in tf_regimes):
            return MarketRegime.HIGH_VOLATILITY

        # For trend: require at least 2 TFs to agree on direction
        bullish = [r for r in tf_regimes if r in (
            MarketRegime.STRONG_UPTREND, MarketRegime.WEAK_UPTREND)]
        bearish = [r for r in tf_regimes if r in (
            MarketRegime.STRONG_DOWNTREND, MarketRegime.WEAK_DOWNTREND)]
        ranging = [r for r in tf_regimes if r == MarketRegime.RANGING]

        total = len(tf_regimes)

        # Majority bullish
        if len(bullish) >= total / 2:
            # If any TF says strong uptrend, use strong; otherwise weak
            if any(r == MarketRegime.STRONG_UPTREND for r in bullish):
                return MarketRegime.STRONG_UPTREND
            return MarketRegime.WEAK_UPTREND

        # Majority bearish
        if len(bearish) >= total / 2:
            if any(r == MarketRegime.STRONG_DOWNTREND for r in bearish):
                return MarketRegime.STRONG_DOWNTREND
            return MarketRegime.WEAK_DOWNTREND

        # Majority ranging
        if len(ranging) >= total / 2:
            return MarketRegime.RANGING

        # No consensus — default to ranging (safest)
        return MarketRegime.RANGING

    async def detect_and_emit(self, df: pd.DataFrame) -> MarketRegime:
        """Detect regime and emit event if changed."""
        old = self._current_regime
        new = self.detect(df)
        if new != old and self._event_bus:
            await self._event_bus.publish(
                make_event(
                    EventType.REGIME_CHANGE,
                    "regime_detector",
                    {"old": old, "new": new, "strategies": STRATEGY_MAP.get(new, [])},
                )
            )
        return new

    def _classify_raw(self, df: pd.DataFrame, latest: pd.Series) -> MarketRegime:
        """Single-bar classification (before confirmation filter)."""

        # 1. CRASH — price dropped > 10% in last 24 candles on 1h
        if len(df) >= 24:
            pct_24 = (latest["close"] - df.iloc[-24]["close"]) / df.iloc[-24]["close"] * 100
            if pct_24 < -10:
                return MarketRegime.CRASH

        adx = latest.get("adx_14", 0)
        if pd.isna(adx):
            adx = 0

        ema_50 = latest.get("ema_50", float("nan"))
        ema_200 = latest.get("ema_200", float("nan"))
        close = latest["close"]

        # ATR percentile for volatility check
        atr_pct = latest.get("atr_pct", 0)
        if not pd.isna(atr_pct) and len(df) >= 100:
            atr_series = df["atr_pct"].dropna()
            if len(atr_series) > 20:
                p80 = float(np.percentile(atr_series, 80))
                if atr_pct > p80:
                    # Also check BB width
                    bb_width = latest.get("bb_width", 0)
                    if not pd.isna(bb_width):
                        bw_series = df["bb_width"].dropna()
                        if len(bw_series) > 20:
                            p75 = float(np.percentile(bw_series, 75))
                            if bb_width > p75:
                                return MarketRegime.HIGH_VOLATILITY

        # 2. Trending vs ranging via ADX
        if adx > 30:
            if not pd.isna(ema_50) and not pd.isna(ema_200):
                if close > ema_50 > ema_200:
                    return MarketRegime.STRONG_UPTREND
                elif close < ema_50 < ema_200:
                    return MarketRegime.STRONG_DOWNTREND
            if not pd.isna(ema_50):
                if close > ema_50:
                    return MarketRegime.STRONG_UPTREND
                else:
                    return MarketRegime.STRONG_DOWNTREND

        if 20 <= adx <= 30:
            if not pd.isna(ema_50):
                if close > ema_50:
                    return MarketRegime.WEAK_UPTREND
                else:
                    return MarketRegime.WEAK_DOWNTREND

        # ADX < 20 — ranging
        return MarketRegime.RANGING
