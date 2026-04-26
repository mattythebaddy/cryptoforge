"""Volume-specific indicators and analysis."""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta


class VolumeIndicators:
    """Extended volume analysis beyond what TechnicalIndicators provides."""

    @staticmethod
    def volume_profile(df: pd.DataFrame, bins: int = 20) -> pd.DataFrame:
        """
        Calculate volume profile — volume distributed across price levels.
        Returns DataFrame with price_level and volume columns.
        """
        if len(df) < 2:
            return pd.DataFrame(columns=["price_level", "volume"])

        price_min = df["low"].min()
        price_max = df["high"].max()
        bin_size = (price_max - price_min) / bins

        levels = []
        for i in range(bins):
            low = price_min + i * bin_size
            high = low + bin_size
            mask = (df["close"] >= low) & (df["close"] < high)
            vol = df.loc[mask, "volume"].sum()
            levels.append({"price_level": (low + high) / 2, "volume": vol})

        return pd.DataFrame(levels)

    @staticmethod
    def poc(df: pd.DataFrame, bins: int = 20) -> float | None:
        """Point of Control — price level with most volume."""
        vp = VolumeIndicators.volume_profile(df, bins)
        if vp.empty:
            return None
        return float(vp.loc[vp["volume"].idxmax(), "price_level"])

    @staticmethod
    def buy_volume_ratio(df: pd.DataFrame) -> pd.Series:
        """
        Estimate buy/sell volume ratio from candle body position.
        Close near high → more buying. Close near low → more selling.
        """
        rng = df["high"] - df["low"]
        rng = rng.replace(0, float("nan"))
        return (df["close"] - df["low"]) / rng

    @staticmethod
    def is_volume_climax(df: pd.DataFrame, threshold: float = 3.0) -> pd.Series:
        """Detect volume climax (volume > threshold × SMA20)."""
        sma = ta.sma(df["volume"], length=20)
        if sma is None:
            return pd.Series(False, index=df.index)
        return df["volume"] > (sma * threshold)
