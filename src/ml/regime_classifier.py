"""ML-based regime classification using HMM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)


class MLRegimeClassifier:
    """
    Uses Hidden Markov Model to detect market regimes.
    Falls back to rule-based detector if not trained.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._trained = False

    def train(self, returns: np.ndarray, n_states: int = 3) -> None:
        """Train 3-state Gaussian HMM on log returns."""
        try:
            from hmmlearn.hmm import GaussianHMM

            model = GaussianHMM(
                n_components=n_states,
                covariance_type="full",
                n_iter=100,
                random_state=42,
            )
            X = returns.reshape(-1, 1)
            model.fit(X)
            self._model = model
            self._trained = True
            log.info("ml_regime.trained", states=n_states, samples=len(returns))
        except ImportError:
            log.warning("ml_regime.hmmlearn_not_installed")
        except Exception:
            log.exception("ml_regime.train_failed")

    def predict_regime(self, recent_returns: np.ndarray) -> int:
        """Return current regime state (0, 1, 2). Returns -1 if not trained."""
        if not self._trained or self._model is None:
            return -1
        try:
            X = recent_returns.reshape(-1, 1)
            states = self._model.predict(X)
            return int(states[-1])
        except Exception:
            log.exception("ml_regime.predict_failed")
            return -1

    def save(self, path: str = "models/regime_hmm.pkl") -> None:
        if self._model:
            import joblib

            Path(path).parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(self._model, path)

    def load(self, path: str = "models/regime_hmm.pkl") -> bool:
        try:
            import joblib

            self._model = joblib.load(path)
            self._trained = True
            return True
        except Exception:
            return False
