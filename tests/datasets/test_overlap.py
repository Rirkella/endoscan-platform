"""overlap_computer: compound-level intersection counts."""

from __future__ import annotations

from types import SimpleNamespace


def test_overlap_counts(er_artifacts: SimpleNamespace) -> None:
    overlap = er_artifacts.overlap
    assert overlap.n_labeled == 7  # C1..C7 (incl. conflicted C7)
    assert overlap.n_with_signature == 7
    assert overlap.n_overlap == 7
    # per-class excludes the conflicted compound.
    assert overlap.per_class_overlap == {1: 3, 0: 3}


def test_overlap_keys_are_inchikeys(er_artifacts: SimpleNamespace) -> None:
    for key in er_artifacts.overlap.compound_keys:
        assert len(key) == 27 and key.count("-") == 2
