"""Nested grouped CV + held-out: structure, determinism, leakage guards."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from endoscan_core.training.nested_cv import (
    _assert_group_disjoint,
    holdout_group_eval,
    nested_group_cv,
)

MODELS = ["ridge_logreg", "gradient_boosting"]


def _synthetic(n_groups: int = 6, per: int = 2):
    rows, groups, ys = [], [], []
    for g in range(n_groups):
        for j in range(per):
            rows.append({"f0": float(np.sin(g + j)), "f1": float(g * 0.1 + j)})
            groups.append(g)
            ys.append((g + j) % 2)
    return pd.DataFrame(rows), pd.Series(ys), groups


def test_group_disjoint_guard() -> None:
    _assert_group_disjoint([0, 0, 1, 1], [0, 1], [2, 3])  # disjoint -> ok
    with pytest.raises(AssertionError):
        _assert_group_disjoint([0, 0, 1, 1], [0, 1], [1, 2])  # group 0 in both


def test_nested_structure_and_determinism() -> None:
    X, y, groups = _synthetic()
    r1 = nested_group_cv(
        X, y, groups, MODELS, outer_splits=3, inner_splits=2, seed=0, tolerance=0.02
    )
    r2 = nested_group_cv(
        X, y, groups, MODELS, outer_splits=3, inner_splits=2, seed=0, tolerance=0.02
    )
    assert r1.outer_splits == 3
    assert len(r1.per_fold) == 3
    assert all(f.selected_model in MODELS for f in r1.per_fold)
    assert r1.outer_metrics.n_samples == len(y)
    # Deterministic.
    assert r1.outer_metrics.auroc == r2.outer_metrics.auroc
    assert r1.selected_models == r2.selected_models


def test_nested_outer_splits_clamped_to_groups() -> None:
    X, y, groups = _synthetic(n_groups=3, per=2)
    result = nested_group_cv(
        X, y, groups, MODELS, outer_splits=10, inner_splits=2, seed=0, tolerance=0.02
    )
    assert result.outer_splits == 3  # clamped to the number of groups


def test_holdout_is_group_disjoint_and_scores() -> None:
    X, y, groups = _synthetic(n_groups=8, per=2)
    result = holdout_group_eval(
        X, y, groups, MODELS, test_size=0.25, inner_splits=2, seed=0, tolerance=0.02
    )
    assert result.selected_model in MODELS
    assert result.n_test > 0
    assert 0.0 <= result.metrics.auroc <= 1.0
