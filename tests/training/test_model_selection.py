"""Scorecard computation + simplicity-aware selection rule."""

from __future__ import annotations

import numpy as np
import pandas as pd

from endoscan_core.training.model_selection import ModelScore, score_candidates, select_model


def _score(name: str, auroc: float, auprc: float, interp: int, simp: int) -> ModelScore:
    return ModelScore(
        name=name,
        auroc=auroc,
        auprc=auprc,
        balanced_accuracy=0.5,
        brier_score=0.2,
        auroc_fold_std=0.0,
        train_seconds=0.0,
        infer_seconds=0.0,
        interpretability_tier=interp,
        simplicity_tier=simp,
    )


def test_prefers_simpler_model_within_tolerance() -> None:
    scores = [
        _score("gradient_boosting", 0.80, 0.80, 3, 3),
        _score("ridge_logreg", 0.79, 0.79, 1, 1),  # within 0.02 of best
    ]
    assert select_model(scores, tolerance=0.02) == "ridge_logreg"


def test_picks_better_model_outside_tolerance() -> None:
    scores = [
        _score("gradient_boosting", 0.90, 0.90, 3, 3),
        _score("ridge_logreg", 0.70, 0.70, 1, 1),  # far worse
    ]
    assert select_model(scores, tolerance=0.02) == "gradient_boosting"


def test_linear_tiebreak_is_deterministic_by_name() -> None:
    scores = [
        _score("ridge_logreg", 0.8, 0.8, 1, 1),
        _score("elastic_net_logreg", 0.8, 0.8, 1, 1),
        _score("lasso_logreg", 0.8, 0.8, 1, 1),
    ]
    assert select_model(scores, tolerance=0.02) == "elastic_net_logreg"


def _synthetic():
    rows, groups, ys = [], [], []
    for g in range(6):
        for j in range(2):
            rows.append({"f0": float(np.sin(g + j)), "f1": float(g * 0.1 + j)})
            groups.append(g)
            ys.append((g + j) % 2)
    return pd.DataFrame(rows), pd.Series(ys), groups


def test_score_candidates_populates_all_columns() -> None:
    X, y, groups = _synthetic()
    scores = score_candidates(X, y, groups, ["ridge_logreg", "random_forest"], n_splits=2, seed=0)
    assert {s.name for s in scores} == {"ridge_logreg", "random_forest"}
    for s in scores:
        assert 0.0 <= s.auroc <= 1.0
        assert 0.0 <= s.auprc <= 1.0
        assert s.train_seconds >= 0.0 and s.infer_seconds >= 0.0
        assert s.interpretability_tier >= 1 and s.simplicity_tier >= 1
