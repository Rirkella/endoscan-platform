"""Compound-level cross-validation splits.

Endpoint models MUST be cross-validated with the compound-level groups produced
by the M2 ``candidate_table_builder`` (never random/stratified row CV), so M2's
no-leakage guarantee carries into model selection. This module wraps
``GroupKFold`` and guards that no group spans train/test.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from sklearn.model_selection import GroupKFold


def n_splits_for_groups(groups: Sequence[int], requested: int) -> int:
    """Clamp the requested split count to the number of distinct groups (>= 2)."""
    n_groups = len(set(groups))
    if n_groups < 2:
        raise ValueError(f"Need at least 2 compound groups for grouped CV, got {n_groups}")
    return min(requested, n_groups)


def grouped_cv_splits(groups: Sequence[int], n_splits: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return GroupKFold (train_idx, test_idx) folds and assert no group leaks."""
    groups = list(groups)
    splitter = GroupKFold(n_splits=n_splits)
    placeholder = np.zeros((len(groups), 1))
    folds = list(splitter.split(placeholder, groups=groups))
    for train_idx, test_idx in folds:
        train_groups = {groups[i] for i in train_idx}
        test_groups = {groups[i] for i in test_idx}
        if train_groups & test_groups:
            raise AssertionError("GroupKFold produced a fold with a group in both sides")
    return folds
