"""ML feature engineering pipeline — all features lagged to prevent look-ahead bias."""

from __future__ import annotations

import numpy as np
import pandas as pd


class FeatureEngine:
    """Creates ML-ready feature matrices from OHLCV + indicator data."""

    def build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build and return feature matrix. All features shifted by 1 to prevent lookahead."""
        features = pd.DataFrame(index=df.index)

        # Price features (lagged)
        for period in (1, 5, 10, 20):
            features[f"returns_{period}"] = np.log(df["close"] / df["close"].shift(period)).shift(1)

        for col in ("ema_21", "ema_50", "ema_200"):
            if col in df.columns:
                features[f"price_vs_{col}"] = (df["close"] / df[col]).shift(1)

        features["higher_high"] = (df["high"] > df["high"].shift(1)).astype(int).shift(1)
        features["lower_low"] = (df["low"] < df["low"].shift(1)).astype(int).shift(1)
        features["gap_pct"] = ((df["open"] - df["close"].shift(1)) / df["close"].shift(1)).shift(1)

        # Indicator features (lagged)
        for col in ("rsi_14", "rsi_7", "adx_14", "bb_pct", "bb_width", "atr_pct"):
            if col in df.columns:
                features[col] = df[col].shift(1)

        if "macd_hist" in df.columns:
            features["macd_hist"] = df["macd_hist"].shift(1)
            features["macd_hist_change"] = df["macd_hist"].diff().shift(1)

        if "obv" in df.columns:
            features["obv_change_pct"] = df["obv"].pct_change().shift(1)

        # Volume features (lagged)
        if "volume_ratio" in df.columns:
            features["volume_ratio"] = df["volume_ratio"].shift(1)
        features["volume_change_pct"] = df["volume"].pct_change().shift(1)

        if "high" in df.columns and "low" in df.columns:
            rng = df["high"] - df["low"]
            rng = rng.replace(0, np.nan)
            features["buy_volume_ratio"] = ((df["close"] - df["low"]) / rng).shift(1)

        # Time features (cyclical encoding)
        if "time" in df.columns:
            dt = pd.to_datetime(df["time"])
            hour = dt.dt.hour
            features["hour_sin"] = np.sin(2 * np.pi * hour / 24)
            features["hour_cos"] = np.cos(2 * np.pi * hour / 24)
            dow = dt.dt.dayofweek
            features["dow_sin"] = np.sin(2 * np.pi * dow / 7)
            features["dow_cos"] = np.cos(2 * np.pi * dow / 7)

        # Drop NaN rows (warmup)
        features = features.dropna()
        return features
