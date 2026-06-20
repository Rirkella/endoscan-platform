"""Train an endpoint model from an M2 candidate table.

Two model types only (no AutoML zoo): elastic-net logistic regression and
gradient boosting, each a scaler+classifier `Pipeline`. Selection is by pooled
out-of-fold (OOF) AUROC over compound-level GroupKFold folds (tie-break AUPRC);
the selected model is then refit on all rows. Everything is deterministic under a
single fixed seed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .evaluate_endpoint import EvalMetrics, evaluate_endpoint
from .splits import grouped_cv_splits, n_splits_for_groups

SEED = 0
MODEL_NAMES = ("elastic_net_logreg", "gradient_boosting")


def build_model(name: str, seed: int = SEED) -> Pipeline:
    """Build a scaler+classifier pipeline for one of the two supported models."""
    if name == "elastic_net_logreg":
        # Elastic-net via saga: l1_ratio mixes L1/L2 (penalty= is deprecated in
        # recent scikit-learn; l1_ratio alone selects elastic-net).
        clf = LogisticRegression(
            solver="saga",
            l1_ratio=0.5,
            C=1.0,
            max_iter=5000,
            random_state=seed,
        )
    elif name == "gradient_boosting":
        clf = GradientBoostingClassifier(random_state=seed)
    else:
        raise ValueError(f"Unknown model {name!r}; supported: {MODEL_NAMES}")
    return Pipeline([("scaler", StandardScaler()), ("clf", clf)])


@dataclass
class CandidateModelResult:
    name: str
    oof_metrics: EvalMetrics


@dataclass
class TrainResult:
    selected_model_name: str
    fitted_model: Pipeline
    metrics: EvalMetrics
    n_splits: int
    n_samples: int
    n_groups: int
    candidates: list[CandidateModelResult]


def _oof_predictions(
    name: str,
    X: pd.DataFrame,
    y: pd.Series,
    folds: list[tuple[np.ndarray, np.ndarray]],
    seed: int,
) -> np.ndarray:
    oof = np.full(len(y), np.nan)
    for train_idx, test_idx in folds:
        model = build_model(name, seed=seed)
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        oof[test_idx] = model.predict_proba(X.iloc[test_idx])[:, 1]
    return oof


def train_endpoint(
    X: pd.DataFrame,
    y: pd.Series,
    groups: Sequence[int],
    model_names: Sequence[str] = MODEL_NAMES,
    n_splits_requested: int = 5,
    seed: int = SEED,
) -> TrainResult:
    """Select the better of the two models by OOF AUROC and refit it on all rows."""
    groups = list(groups)
    n_splits = n_splits_for_groups(groups, n_splits_requested)
    folds = grouped_cv_splits(groups, n_splits)

    candidates: list[CandidateModelResult] = []
    for name in model_names:
        oof = _oof_predictions(name, X, y, folds, seed)
        candidates.append(CandidateModelResult(name=name, oof_metrics=evaluate_endpoint(y, oof)))

    # Select by OOF AUROC, tie-break AUPRC, then name for full determinism.
    best = max(
        candidates,
        key=lambda c: (c.oof_metrics.auroc, c.oof_metrics.auprc, c.name),
    )

    fitted = build_model(best.name, seed=seed)
    fitted.fit(X, y)

    return TrainResult(
        selected_model_name=best.name,
        fitted_model=fitted,
        metrics=best.oof_metrics,
        n_splits=n_splits,
        n_samples=len(y),
        n_groups=len(set(groups)),
        candidates=candidates,
    )
