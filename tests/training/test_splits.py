"""Grouped CV: clamping and the compound-level no-leakage guarantee."""

from __future__ import annotations

from endoscan_core.datasets import CandidateTable
from endoscan_core.training.splits import grouped_cv_splits, n_splits_for_groups


def test_n_splits_clamped_to_group_count() -> None:
    assert n_splits_for_groups([0, 0, 1, 1], 5) == 2
    assert n_splits_for_groups([0, 1, 2, 3], 3) == 3


def test_grouped_cv_no_compound_spans_train_and_test(candidate_table: CandidateTable) -> None:
    groups = candidate_table.metadata["split_group"].tolist()
    n_splits = n_splits_for_groups(groups, 5)
    folds = grouped_cv_splits(groups, n_splits)
    assert len(folds) == 2
    for train_idx, test_idx in folds:
        train_groups = {groups[i] for i in train_idx}
        test_groups = {groups[i] for i in test_idx}
        assert not (train_groups & test_groups)
