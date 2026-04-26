"""Technical indicators wrapping pandas-ta with caching."""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta
import structlog

log = structlog.get_logger(__name__)


class TechnicalIndicators:
    """Calculate indicators on OHLCV DataFrames."""

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add all indicator columns to the DataFrame."""
        if len(df) < 2:
            return df

        df = df.copy()

        # --- Trend ---
        for period in (9, 21, 50, 200):
            col = f"ema_{period}"
            if len(df) >= period:
                df[col] = ta.ema(df["close"], length=period)
            else:
                df[col] = float("nan")

        for period in (20, 50):
            col = f"sma_{period}"
            if len(df) >= period:
                df[col] = ta.sma(df["close"], length=period)
            else:
                df[col] = float("nan")

        if len(df) >= 14:
            adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
            if adx_df is not None:
                df["adx_14"] = adx_df.iloc[:, 0]
            else:
                df["adx_14"] = float("nan")
        else:
            df["adx_14"] = float("nan")

        if len(df) >= 10:
            st = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
            if st is not None:
                df["supertrend_10_3"] = st.iloc[:, 0]
            else:
                df["supertrend_10_3"] = float("nan")
        else:
            df["supertrend_10_3"] = float("nan")

        # --- Momentum ---
        for period in (14, 7):
            col = f"rsi_{period}"
            if len(df) >= period + 1:
                df[col] = ta.rsi(df["close"], length=period)
            else:
                df[col] = float("nan")

        if len(df) >= 33:
            macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
            if macd_df is not None:
                df["macd"] = macd_df.iloc[:, 0]
                df["macd_signal"] = macd_df.iloc[:, 2]
                df["macd_hist"] = macd_df.iloc[:, 1]
            else:
                df["macd"] = df["macd_signal"] = df["macd_hist"] = float("nan")
        else:
            df["macd"] = df["macd_signal"] = df["macd_hist"] = float("nan")

        if len(df) >= 14:
            stoch = ta.stoch(df["high"], df["low"], df["close"], k=14, d=3, smooth_k=3)
            if stoch is not None:
                df["stoch_k"] = stoch.iloc[:, 0]
                df["stoch_d"] = stoch.iloc[:, 1]
            else:
                df["stoch_k"] = df["stoch_d"] = float("nan")
        else:
            df["stoch_k"] = df["stoch_d"] = float("nan")

        if len(df) >= 20:
            df["cci_20"] = ta.cci(df["high"], df["low"], df["close"], length=20)
        else:
            df["cci_20"] = float("nan")

        if len(df) >= 14:
            df["mfi_14"] = ta.mfi(df["high"], df["low"], df["close"], df["volume"], length=14)
            df["willr_14"] = ta.willr(df["high"], df["low"], df["close"], length=14)
        else:
            df["mfi_14"] = df["willr_14"] = float("nan")

        # --- Volatility ---
        if len(df) >= 20:
            bb = ta.bbands(df["close"], length=20, std=2.0)
            if bb is not None:
                df["bb_lower"] = bb.iloc[:, 0]
                df["bb_mid"] = bb.iloc[:, 1]
                df["bb_upper"] = bb.iloc[:, 2]
                df["bb_width"] = bb.iloc[:, 3] if bb.shape[1] > 3 else (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
                df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
            else:
                df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = float("nan")
                df["bb_width"] = df["bb_pct"] = float("nan")
        else:
            df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = float("nan")
            df["bb_width"] = df["bb_pct"] = float("nan")

        if len(df) >= 14:
            df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)
            df["atr_pct"] = (df["atr_14"] / df["close"]) * 100
        else:
            df["atr_14"] = df["atr_pct"] = float("nan")

        if len(df) >= 20:
            kc = ta.kc(df["high"], df["low"], df["close"], length=20, scalar=1.5)
            if kc is not None:
                df["keltner_lower"] = kc.iloc[:, 0]
                df["keltner_upper"] = kc.iloc[:, 2]
            else:
                df["keltner_lower"] = df["keltner_upper"] = float("nan")
        else:
            df["keltner_lower"] = df["keltner_upper"] = float("nan")

        # --- Volume ---
        if len(df) >= 2:
            df["obv"] = ta.obv(df["close"], df["volume"])
        else:
            df["obv"] = float("nan")

        if len(df) >= 1:
            df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
        else:
            df["vwap"] = float("nan")

        if len(df) >= 20:
            df["volume_sma_20"] = ta.sma(df["volume"], length=20)
            df["volume_ratio"] = df["volume"] / df["volume_sma_20"]
        else:
            df["volume_sma_20"] = df["volume_ratio"] = float("nan")

        return df
