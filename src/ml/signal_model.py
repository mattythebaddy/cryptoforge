"""ML-based entry/exit signal scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)


class SignalModel:
    """XGBoost-based signal scoring. Optional enhancement over rule-based strategies."""

    def __init__(self) -> None:
        self._model: Any = None
        self._trained = False
        self._feature_columns: list[str] = []

    def train(self, X: pd.DataFrame, y: pd.Series) -> dict[str, float]:
        """
        Train XGBoost classifier.
        y: 1 = profitable trade, 0 = losing trade
        Returns metrics dict.
        """
        try:
            from sklearn.model_selection import TimeSeriesSplit
            from xgboost import XGBClassifier

            self._feature_columns = list(X.columns)

            model = XGBClassifier(
                n_estimators=200,
                max_depth=5,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42,
                eval_metric="logloss",
            )

            # Time-series cross-validation
            tscv = TimeSeriesSplit(n_splits=5)
            scores = []
            for train_idx, test_idx in tscv.split(X):
                X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
                y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
                model.fit(X_train, y_train)
                scores.append(model.score(X_test, y_test))

            # Final fit on all data
            model.fit(X, y)
            self._model = model
            self._trained = True

            metrics = {
                "accuracy": float(np.mean(scores)),
                "std": float(np.std(scores)),
                "features": len(self._feature_columns),
            }
            log.info("signal_model.trained", **metrics)
            return metrics

        except ImportError:
            log.warning("signal_model.xgboost_not_installed")
            return {}
        except Exception:
            log.exception("signal_model.train_failed")
            return {}

    def predict_probability(self, features: pd.DataFrame) -> float:
        """Return probability (0-1) that a trade at this point would be profitable."""
        if not self._trained or self._model is None:
            return 0.5
        try:
            # Align features
            X = features[self._feature_columns] if self._feature_columns else features
            proba = self._model.predict_proba(X.iloc[[-1]])
            return float(proba[0][1])
        except Exception:
            return 0.5

    def save(self, path: str = "models/signal_xgb.json") -> None:
        if self._model:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._model.save_model(path)

    def load(self, path: str = "models/signal_xgb.json") -> bool:
        try:
            from xgboost import XGBClassifier

            self._model = XGBClassifier()
            self._model.load_model(path)
            self._trained = True
            return True
        except Exception:
            return False
