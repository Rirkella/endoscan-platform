"""Nested (and held-out) compound-level evaluation.

The honest generalization estimate for a *model-selection procedure*. Default is
nested grouped CV: the **outer** GroupKFold gives the unbiased estimate, while
selection (the scorecard + simplicity-aware rule) runs only on each outer-train
fold via an **inner** GroupKFold. The reported metrics come from pooled
**outer-test** predictions — the outer-test rows never participate in selection,
which removes the selection-optimism bias.

Leakage guarantee (explicit): EVERY split at EVERY level is compound-level /
group-disjoint. A compound's rows are confined to one outer fold (outer-train and
outer-test groups are disjoint), and within an outer-train fold the inner splits
use the same compound groups, so a compound never appears in both inner-train and
inner-validation. This module asserts outer disjointness directly; the inner
splits inherit it from `grouped_cv_splits`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from .evaluate_endpoint import EvalMetrics, evaluate_endpoint
from .model_selection import score_candidates, select_model
from .splits import grouped_cv_splits, n_splits_for_groups
from .train_endpoint import build_model


@dataclass
class NestedFoldResult:
    fold: int
    selected_model: str
    n_test: int


@dataclass
class NestedCVResult:
    outer_metrics: EvalMetrics  # honest, pooled outer-test estimate
    per_fold: list[NestedFoldResult]
    outer_splits: int
    inner_splits: int

    @property
    def selected_models(self) -> list[str]:
        return [f.selected_model for f in self.per_fold]


@dataclass
class HoldoutResult:
    metrics: EvalMetrics  # honest, single group-disjoint test set
    selected_model: str
    n_test: int


def _assert_group_disjoint(groups: list[int], train_idx, test_idx) -> None:
    train_groups = {groups[i] for i in train_idx}
    test_groups = {groups[i] for i in test_idx}
    if train_groups & test_groups:
        raise AssertionError("Compound group appears in both train and test (leakage)")


def nested_group_cv(
    X: pd.DataFrame,
    y: pd.Series,
    groups: Sequence[int],
    model_names: Sequence[str],
    *,
    outer_splits: int,
    inner_splits: int,
    seed: int,
    tolerance: float,
) -> NestedCVResult:
    """Nested grouped CV honest estimate of the selection procedure."""
    groups = list(groups)
    outer_n = n_splits_for_groups(groups, outer_splits)
    outer_folds = grouped_cv_splits(groups, outer_n)

    oof = np.full(len(y), np.nan)
    per_fold: list[NestedFoldResult] = []

    for fold, (train_idx, test_idx) in enumerate(outer_folds):
        _assert_group_disjoint(groups, train_idx, test_idx)
        train_groups = [groups[i] for i in train_idx]

        # Selection happens ONLY on the outer-train fold (inner grouped CV).
        inner_n = n_splits_for_groups(train_groups, inner_splits)
        scores = score_candidates(
            X.iloc[train_idx].reset_index(drop=True),
            y.iloc[train_idx].reset_index(drop=True),
            train_groups,
            model_names,
            inner_n,
            seed,
        )
        selected = select_model(scores, tolerance)

        model = build_model(selected, seed=seed)
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        oof[test_idx] = model.predict_proba(X.iloc[test_idx])[:, 1]
        per_fold.append(NestedFoldResult(fold=fold, selected_model=selected, n_test=len(test_idx)))

    return NestedCVResult(
        outer_metrics=evaluate_endpoint(y, oof),
        per_fold=per_fold,
        outer_splits=outer_n,
        inner_splits=inner_splits,
    )


def holdout_group_eval(
    X: pd.DataFrame,
    y: pd.Series,
    groups: Sequence[int],
    model_names: Sequence[str],
    *,
    test_size: float,
    inner_splits: int,
    seed: int,
    tolerance: float,
) -> HoldoutResult:
    """Honest estimate from a single compound-level held-out test set."""
    groups = list(groups)
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_idx, test_idx = next(splitter.split(X, y, groups))
    _assert_group_disjoint(groups, train_idx, test_idx)

    train_groups = [groups[i] for i in train_idx]
    inner_n = n_splits_for_groups(train_groups, inner_splits)
    scores = score_candidates(
        X.iloc[train_idx].reset_index(drop=True),
        y.iloc[train_idx].reset_index(drop=True),
        train_groups,
        model_names,
        inner_n,
        seed,
    )
    selected = select_model(scores, tolerance)

    model = build_model(selected, seed=seed)
    model.fit(X.iloc[train_idx], y.iloc[train_idx])
    proba = model.predict_proba(X.iloc[test_idx])[:, 1]
    return HoldoutResult(
        metrics=evaluate_endpoint(y.iloc[test_idx], proba),
        selected_model=selected,
        n_test=len(test_idx),
    )
