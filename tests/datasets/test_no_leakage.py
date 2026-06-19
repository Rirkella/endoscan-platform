"""Compound-level no-leakage guarantee + the report's leakage detector."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from endoscan_core.datasets import dataset_quality_report
from endoscan_core.datasets.candidate_table import CandidateTable


def test_no_compound_spans_groups(er_artifacts: SimpleNamespace) -> None:
    metadata = er_artifacts.table.metadata
    spans = metadata.groupby("compound_key")["split_group"].nunique()
    assert (spans == 1).all()


def test_leakage_detector_flags_injected_leak(
    tmp_path: Path, er_artifacts: SimpleNamespace
) -> None:
    table = er_artifacts.table
    metadata = table.metadata.copy()

    # Force a multi-row compound to span two groups.
    counts = metadata["compound_key"].value_counts()
    multi_key = counts[counts > 1].index[0]
    rows = list(metadata.index[metadata["compound_key"] == multi_key])
    other_group = metadata.loc[rows[1], "split_group"]
    metadata.loc[rows[0], "split_group"] = int(other_group) + 1

    leaked = CandidateTable(
        X=table.X,
        y=table.y,
        metadata=metadata,
        split_plan=table.split_plan,
        feature_names=table.feature_names,
        excluded_conflicts=table.excluded_conflicts,
    )
    overlap = er_artifacts.overlap
    report = dataset_quality_report(
        "ER",
        leaked,
        er_artifacts.labels,
        er_artifacts.signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        tmp_path,
        write_card=False,
    )
    assert report.compound_level_leakage is True
    assert multi_key in report.offending_compounds
