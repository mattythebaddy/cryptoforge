"""Fear & Greed Index integration."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_FNG_URL = "https://api.alternative.me/fng/"
_CACHE_TTL = 3600  # cache for 1 hour


class SentimentAnalyzer:
    """Fetches Fear & Greed Index and computes position size multipliers."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0

    async def get_fear_greed_index(self) -> dict[str, Any]:
        """
        Returns:
            {"value": 25, "classification": "Extreme Fear", "timestamp": "..."}
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < _CACHE_TTL:
            return self._cache

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(_FNG_URL)
                resp.raise_for_status()
                data = resp.json()
                entry = data.get("data", [{}])[0]
                result = {
                    "value": int(entry.get("value", 50)),
                    "classification": entry.get("value_classification", "Neutral"),
                    "timestamp": entry.get("timestamp", ""),
                }
                self._cache = result
                self._cache_ts = now
                log.debug("sentiment.fng_fetched", value=result["value"])
                return result
        except Exception:
            log.exception("sentiment.fng_fetch_failed")
            return {"value": 50, "classification": "Neutral", "timestamp": ""}

    @staticmethod
    def get_fear_multiplier(fng_value: int) -> float:
        """
        Position size multiplier based on Fear & Greed.
        Extreme fear = buy more, extreme greed = don't buy.
        """
        if fng_value <= 10:
            return 2.0
        elif fng_value <= 25:
            return 1.5
        elif fng_value <= 45:
            return 1.2
        elif fng_value <= 55:
            return 1.0
        elif fng_value <= 75:
            return 0.7
        elif fng_value <= 90:
            return 0.3
        else:
            return 0.0  # extreme greed — no new entries
