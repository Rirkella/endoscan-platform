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
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

from .evaluate_endpoint import EvalMetrics, evaluate_endpoint
from .splits import grouped_cv_splits, n_splits_for_groups

SEED = 0

#: All sklearn candidate models (no neural nets; no XGBoost/LightGBM). The three
#: logistic-regression variants differ only by their L1/L2 mix (l1_ratio).
MODEL_NAMES = (
    "ridge_logreg",  # l1_ratio=0 (L2)
    "lasso_logreg",  # l1_ratio=1 (L1)
    "elastic_net_logreg",  # l1_ratio=0.5
    "random_forest",
    "gradient_boosting",
)

_L1_RATIO = {"ridge_logreg": 0.0, "lasso_logreg": 1.0, "elastic_net_logreg": 0.5}

#: Static (interpretability_tier, simplicity_tier) per model; lower = simpler /
#: more interpretable / cheaper to deploy. Used by the simplicity-aware selector.
_MODEL_TIERS = {
    "ridge_logreg": (1, 1),
    "lasso_logreg": (1, 1),
    "elastic_net_logreg": (1, 1),
    "random_forest": (3, 2),
    "gradient_boosting": (3, 3),
}


def model_tiers(name: str) -> tuple[int, int]:
    """Return ``(interpretability_tier, simplicity_tier)`` for a model."""
    if name not in _MODEL_TIERS:
        raise ValueError(f"Unknown model {name!r}; supported: {MODEL_NAMES}")
    return _MODEL_TIERS[name]


def build_model(name: str, seed: int = SEED) -> Pipeline:
    """Build a scaler+classifier pipeline for one of the supported models.

    Class imbalance: LogisticRegression and RandomForest take
    ``class_weight="balanced"`` directly. GradientBoosting has no such parameter,
    so it is balanced per-fit via `fit_balanced` (sample weights computed from the
    fold's TRAINING labels — see that function).
    """
    if name in _L1_RATIO:
        # LR family via saga: l1_ratio mixes L1/L2 (penalty= is deprecated in
        # recent scikit-learn; l1_ratio alone selects the elastic-net family).
        clf = LogisticRegression(
            solver="saga",
            l1_ratio=_L1_RATIO[name],
            C=1.0,
            max_iter=5000,
            random_state=seed,
            class_weight="balanced",
        )
    elif name == "random_forest":
        clf = RandomForestClassifier(n_estimators=200, random_state=seed, class_weight="balanced")
    elif name == "gradient_boosting":
        clf = GradientBoostingClassifier(random_state=seed)
    else:
        raise ValueError(f"Unknown model {name!r}; supported: {MODEL_NAMES}")
    return Pipeline([("scaler", StandardScaler()), ("clf", clf)])


def fit_balanced(model: Pipeline, X: pd.DataFrame, y: pd.Series) -> Pipeline:
    """Fit a candidate pipeline with imbalance handling, leakage-safe.

    For GradientBoosting (no ``class_weight``), balanced ``sample_weight`` is
    computed **from the ``y`` passed to this call** — i.e. the current fold's
    training labels (or the final-refit labels) — and routed to the classifier
    step. It is NEVER precomputed globally, so a test fold's prevalence cannot
    leak into training. LogisticRegression/RandomForest already carry
    ``class_weight="balanced"`` and need no per-fit weights.
    """
    clf = model.named_steps["clf"]
    if isinstance(clf, GradientBoostingClassifier):
        sample_weight = compute_sample_weight(class_weight="balanced", y=y)
        model.fit(X, y, clf__sample_weight=sample_weight)
    else:
        model.fit(X, y)
    return model


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
        fit_balanced(model, X.iloc[train_idx], y.iloc[train_idx])
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
    fit_balanced(fitted, X, y)

    return TrainResult(
        selected_model_name=best.name,
        fitted_model=fitted,
        metrics=best.oof_metrics,
        n_splits=n_splits,
        n_samples=len(y),
        n_groups=len(set(groups)),
        candidates=candidates,
    )
