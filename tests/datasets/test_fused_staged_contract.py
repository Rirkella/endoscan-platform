"""Compound-level FUSED staged-matrix contract (the bug staged/er never caught).

The real post-approval ``lincs.parquet`` is a COMPOUND-LEVEL fused matrix (one row per
compound: an ``compound_id`` InChIKey column + numeric landmark genes), not the
SIGNATURE-level shape the dataset layer fixtures use. These tests prove the fused path:
non-zero overlap, correct per-class counts, the ID column excluded from features,
labels<->signatures joining on InChIKey, fused metadata-coverage that can both pass
AND fail the gate, and back-compat with an existing ``compound_key``-named parquet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from endoscan_core.datasets import (
    GateThresholds,
    SourcesAllowList,
    StagedSourceAdapter,
    candidate_table_builder,
    compound_mapper,
    dataset_quality_report,
    label_retriever,
    overlap_computer,
    quality_gates,
    signature_retriever,
    source_selector,
)
from endoscan_core.datasets.candidate_table import CandidateTable, SplitPlan
from endoscan_core.datasets.label_retriever import LabelSet
from endoscan_core.datasets.quality_report import _metadata_coverage
from endoscan_core.datasets.signature_retriever import SignatureRecord, SignatureSet

FUSED_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "training" / "staged" / "er_fused"

# Relaxed thresholds (the same TEST-ONLY relaxation used by the pipeline tests).
RELAXED = GateThresholds(
    min_compounds_per_class=2,
    min_overlap=4,
    max_duplicate_signature_rate=0.2,
    max_label_conflict_rate=0.2,
    min_metadata_coverage=0.9,
    require_compound_level_split_feasible=True,
)


def _build(allow_list: SourcesAllowList, staged_dir: Path):
    """Run the dataset layer chain over a STAGED fused fixture and return every intermediate."""
    adapter = StagedSourceAdapter(staged_dir)
    target = "ER"
    labels = label_retriever(
        target,
        source_selector(target, allow_list, types=["labels"]),
        adapter,
        allow_list=allow_list,
    )
    signatures = signature_retriever(
        source_selector(target, allow_list, types=["signatures"]), adapter, allow_list=allow_list
    )
    ids = [(r.compound_id, r.compound_id_type) for r in labels.records]
    ids += [(s.compound_id, s.compound_id_type) for s in signatures.records]
    mapping = compound_mapper(
        ids,
        source_selector(target, allow_list, types=["mapping"])[0],
        adapter,
        allow_list=allow_list,
    )
    overlap = overlap_computer(labels, signatures, mapping)
    table = candidate_table_builder(labels, signatures, overlap, mapping, n_groups=3, seed=0)
    return labels, signatures, mapping, overlap, table


def test_fused_signatures_are_compound_level_and_exclude_id(allow_list: SourcesAllowList) -> None:
    _labels, signatures, _mapping, _overlap, table = _build(allow_list, FUSED_DIR)
    # The fused matrix is detected as compound-level (one record per compound).
    assert signatures.granularity == "compound"
    assert len(signatures.records) == 10
    assert all(r.compound_id_type == "inchikey" for r in signatures.records)
    # The ID column is NEVER a numeric feature.
    assert signatures.feature_names == ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]
    assert "compound_id" not in signatures.feature_names
    assert "compound_key" not in signatures.feature_names
    assert list(table.X.columns) == signatures.feature_names


def test_fused_overlap_is_nonzero_with_correct_per_class(allow_list: SourcesAllowList) -> None:
    _labels, _signatures, _mapping, overlap, table = _build(allow_list, FUSED_DIR)
    # Labels (CASRN) resolve to InChIKey via pubchem.csv; signatures key on InChIKey;
    # they join on the SAME InChIKey -> overlap = all 10 compounds (NOT 0).
    assert overlap.n_overlap == 10
    assert overlap.per_class_overlap == {1: 6, 0: 4}
    # One row per compound -> compound-level counts equal the overlap split.
    assert table.metadata["compound_key"].nunique() == 10
    assert len(table.X) == 10


def test_fused_passes_relaxed_gate_and_is_leakage_free(allow_list: SourcesAllowList) -> None:
    _labels, signatures, _mapping, overlap, table = _build(allow_list, FUSED_DIR)
    report = dataset_quality_report(
        "ER",
        table,
        _labels_of(allow_list),
        signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        dataset_cards_dir=Path("/tmp/_fused_cards"),
        write_card=False,
    )
    assert report.per_class_compound_counts == {1: 6, 0: 4}
    # Fused: one row per compound -> no duplicate signatures, trivially leakage-free.
    assert report.duplicate_signature_rate == 0.0
    assert report.compound_level_leakage is False
    assert report.metadata_coverage == 1.0  # clean fixture: all features present
    verdict = quality_gates(report, RELAXED)
    assert verdict.passed is True, verdict.summary


def _labels_of(allow_list: SourcesAllowList) -> LabelSet:
    labels, _s, _m, _o, _t = _build(allow_list, FUSED_DIR)
    return labels


# --- metadata coverage can both PASS and FAIL on the fused path ---------------------


def _fused_table(n: int, n_incomplete: int) -> tuple[CandidateTable, SignatureSet, LabelSet]:
    """A fused candidate table of ``n`` compounds; ``n_incomplete`` have a NaN feature."""
    genes = ["G1", "G2"]
    rows_x, meta_rows, records = [], [], []
    for i in range(n):
        key = f"{chr(ord('A') + i) * 14}-{chr(ord('A') + i) * 10}-N"
        g1 = float("nan") if i < n_incomplete else float(i)
        rows_x.append({"G1": g1, "G2": 1.0})
        meta_rows.append(
            {
                "compound_key": key,
                "inchikey_block1": key[:14],
                "perturbagen_id": key,
                "cell_line": None,
                "dose": None,
                "time": None,
                "split_group": i % 3,
                "label_conflict": False,
            }
        )
        records.append(
            SignatureRecord(
                signature_id=key,
                perturbagen_id=key,
                compound_id=key,
                compound_id_type="inchikey",
                features={"G1": 0.0, "G2": 1.0},
                metadata={},
            )
        )
    X = pd.DataFrame(rows_x, columns=genes)
    y = pd.Series([1 if i % 2 == 0 else 0 for i in range(n)], name="label", dtype="int64")
    groups = {m["compound_key"]: m["split_group"] for m in meta_rows}
    table = CandidateTable(
        X=X,
        y=y,
        metadata=pd.DataFrame(meta_rows),
        split_plan=SplitPlan(n_groups=3, seed=0, groups=groups, folds={}),
        feature_names=genes,
    )
    signatures = SignatureSet(records=records, feature_names=genes, granularity="compound")
    return table, signatures, LabelSet()


def test_fused_metadata_coverage_passes_when_complete() -> None:
    table, signatures, labels = _fused_table(n=10, n_incomplete=0)
    assert _metadata_coverage(table, "compound") == 1.0
    report = dataset_quality_report(
        "ER",
        table,
        labels,
        signatures,
        10,
        10,
        10,
        dataset_cards_dir=Path("/tmp/_fused_cards"),
        write_card=False,
    )
    check = next(
        c for c in quality_gates(report, RELAXED).checks if c.name == "min_metadata_coverage"
    )
    assert check.passed is True and check.observed == 1.0


def test_fused_metadata_coverage_fails_when_features_incomplete() -> None:
    # 2 of 10 compounds have a NaN landmark feature -> coverage 0.8 < 0.9 -> gate FAIL.
    table, signatures, labels = _fused_table(n=10, n_incomplete=2)
    assert _metadata_coverage(table, "compound") == pytest.approx(0.8)
    report = dataset_quality_report(
        "ER",
        table,
        labels,
        signatures,
        10,
        10,
        10,
        dataset_cards_dir=Path("/tmp/_fused_cards"),
        write_card=False,
    )
    check = next(
        c for c in quality_gates(report, RELAXED).checks if c.name == "min_metadata_coverage"
    )
    assert check.passed is False and check.observed == pytest.approx(0.8)


# --- back-compat: an existing on-disk parquet keyed by `compound_key` still loads ----


def test_compound_key_alias_loads_existing_parquet(
    allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    # Re-key the fixture parquet to the LEGACY `compound_key` column (what the already
    # staged data/staged/er/lincs.parquet uses) and copy the label/mapping CSVs.
    fused = pd.read_parquet(FUSED_DIR / "lincs.parquet").rename(
        columns={"compound_id": "compound_key"}
    )
    fused.to_parquet(tmp_path / "lincs.parquet", index=False)
    for name in ("cerapp.csv", "pubchem.csv", "toxcast.csv", "tox21.csv"):
        (tmp_path / name).write_text((FUSED_DIR / name).read_text(), encoding="utf-8")

    _labels, signatures, _mapping, overlap, table = _build(allow_list, tmp_path)
    # The `compound_key` alias is recognized as the fused ID -> identical contract.
    assert signatures.granularity == "compound"
    assert "compound_key" not in signatures.feature_names
    assert overlap.n_overlap == 10
    assert overlap.per_class_overlap == {1: 6, 0: 4}
