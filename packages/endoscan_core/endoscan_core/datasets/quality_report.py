"""Compute a dataset quality report and write a dataset card.

The report quantifies the dataset's fitness (overlap, class balance, duplicate
signatures, label conflicts/confidence, metadata coverage, leakage, split
feasibility). It writes a dataset card from the M1 ``dataset_card.md`` template
but makes NO pass/fail decision — that is the quality gate's job.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ..registry.templates import render_template
from .candidate_table import CandidateTable
from .label_retriever import LabelSet
from .signature_retriever import SignatureSet


class DatasetQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    n_labeled: int
    n_with_signature: int
    n_overlap: int
    n_rows: int
    per_class_counts: dict[int, int] = Field(default_factory=dict)  # row-level (signatures)
    per_class_compound_counts: dict[int, int] = Field(default_factory=dict)  # unique compounds
    minority_class_fraction: float
    duplicate_signature_rate: float
    n_label_conflicts: int
    label_conflict_rate: float
    confidence_summary: dict[str, float] = Field(default_factory=dict)
    metadata_coverage: float
    compound_level_leakage: bool
    offending_compounds: list[str] = Field(default_factory=list)
    split_feasible: bool
    n_groups: int
    dataset_card_path: str | None = None


def _duplicate_signature_rate(table: CandidateTable, granularity: str = "signature") -> float:
    if len(table.metadata) == 0:
        return 0.0
    if granularity == "compound":
        # One fused vector per compound by construction; duplicates = repeated compounds.
        n_total = len(table.metadata)
        n_unique = table.metadata["compound_key"].nunique()
        return (n_total - n_unique) / n_total
    keys = ["compound_key", "cell_line", "dose", "time"]
    n_total = len(table.metadata)
    n_unique = len(table.metadata[keys].drop_duplicates())
    return (n_total - n_unique) / n_total


def _metadata_coverage(table: CandidateTable, granularity: str = "signature") -> float:
    if len(table.metadata) == 0:
        return 0.0
    if granularity == "compound":
        # Per-signature cell/dose/time were consumed by fusion, so coverage here is the
        # fraction of compounds with a resolved identity AND a complete feature vector
        # (no NaN among the landmark genes) — meaningful for a fused matrix, and never
        # spuriously 0 just because the condition columns are gone.
        has_key = table.metadata["compound_key"].notna()
        features_complete = (
            table.X.notna().all(axis=1)
            if len(table.X.columns)
            else pd.Series(True, index=table.metadata.index)
        )
        features_complete = features_complete.reset_index(drop=True)
        complete = has_key.reset_index(drop=True) & features_complete
        return float(complete.mean())
    needed = ["cell_line", "dose", "time"]
    complete = table.metadata[needed].notna().all(axis=1)
    return float(complete.mean())


def _confidence_summary(labels: LabelSet) -> dict[str, float]:
    values = [record.confidence for record in labels.records]
    if not values:
        return {}
    return {
        "min": float(min(values)),
        "mean": float(sum(values) / len(values)),
        "max": float(max(values)),
    }


def _leakage(table: CandidateTable) -> tuple[bool, list[str]]:
    """A compound leaks if its rows occupy more than one split group."""
    if len(table.metadata) == 0:
        return False, []
    spans = table.metadata.groupby("compound_key")["split_group"].nunique()
    offending = sorted(spans[spans > 1].index.tolist())
    return bool(offending), offending


def _per_class_compound_counts(table: CandidateTable) -> dict[int, int]:
    """Unique compounds per label (not signatures)."""
    if len(table.metadata) == 0:
        return {}
    frame = table.metadata.assign(label=table.y.to_numpy()).drop_duplicates("compound_key")
    return {int(k): int(v) for k, v in frame["label"].value_counts().to_dict().items()}


def _split_feasible(table: CandidateTable) -> bool:
    """Each present class must appear in >= 2 distinct groups."""
    if len(table.metadata) == 0:
        return False
    frame = table.metadata.assign(label=table.y.to_numpy())
    for _, group in frame.groupby("label"):
        if group["split_group"].nunique() < 2:
            return False
    return True


def dataset_quality_report(
    target: str,
    table: CandidateTable,
    labels: LabelSet,
    signatures: SignatureSet,
    overlap_n_labeled: int,
    overlap_n_with_signature: int,
    overlap_n_overlap: int,
    dataset_cards_dir: Path,
    write_card: bool = True,
) -> DatasetQualityReport:
    """Build the quality report and (optionally) write the dataset card."""
    per_class = {int(k): int(v) for k, v in table.y.value_counts().to_dict().items()}
    per_class_compounds = _per_class_compound_counts(table)
    n_rows = len(table.y)
    total_compounds = len(table.split_plan.groups)
    minority_fraction = (min(per_class.values()) / sum(per_class.values())) if per_class else 0.0

    n_conflicts = len(table.excluded_conflicts)
    conflict_denominator = total_compounds + n_conflicts
    conflict_rate = (n_conflicts / conflict_denominator) if conflict_denominator else 0.0

    leakage, offending = _leakage(table)

    report = DatasetQualityReport(
        target=target,
        n_labeled=overlap_n_labeled,
        n_with_signature=overlap_n_with_signature,
        n_overlap=overlap_n_overlap,
        n_rows=n_rows,
        per_class_counts=per_class,
        per_class_compound_counts=per_class_compounds,
        minority_class_fraction=minority_fraction,
        duplicate_signature_rate=_duplicate_signature_rate(table, signatures.granularity),
        n_label_conflicts=n_conflicts,
        label_conflict_rate=conflict_rate,
        confidence_summary=_confidence_summary(labels),
        metadata_coverage=_metadata_coverage(table, signatures.granularity),
        compound_level_leakage=leakage,
        offending_compounds=offending,
        split_feasible=_split_feasible(table),
        n_groups=table.split_plan.n_groups,
    )

    if write_card:
        report.dataset_card_path = _write_dataset_card(
            target, table, labels, signatures, report, dataset_cards_dir
        )
    return report


def _write_dataset_card(
    target: str,
    table: CandidateTable,
    labels: LabelSet,
    signatures: SignatureSet,
    report: DatasetQualityReport,
    dataset_cards_dir: Path,
) -> str:
    sources = sorted({record.assay_source for record in labels.records})
    sources.extend(sorted({"lincs"} if signatures.records else set()))
    values = {
        "dataset_id": target,
        "biological_target": target,
        "sources": ", ".join(sources) if sources else "none",
        "compound_counts": (
            f"overlap={report.n_overlap}, "
            f"compounds_per_class={report.per_class_compound_counts}, "
            f"signatures_per_class={report.per_class_counts}"
        ),
        "overlap_summary": (
            f"labeled={report.n_labeled}, with_signature={report.n_with_signature}, "
            f"overlap={report.n_overlap}"
        ),
        "duplicate_summary": f"{report.duplicate_signature_rate:.3f}",
        "label_quality": (
            f"conflicts={report.n_label_conflicts} "
            f"(rate={report.label_conflict_rate:.3f}); confidence={report.confidence_summary}"
        ),
        "metadata_coverage": f"{report.metadata_coverage:.3f}",
        "split_plan": (
            f"compound-level, n_groups={report.n_groups}, "
            f"feasible={report.split_feasible}, leakage={report.compound_level_leakage}"
        ),
        "limitations": (
            "Constructed from a curated allow-list of sources; not a regulatory "
            "dataset. Conflicted-label compounds are excluded. Coverage and class "
            "balance are limited by source overlap."
        ),
    }
    rendered = render_template("dataset_card.md", values)
    dataset_cards_dir.mkdir(parents=True, exist_ok=True)
    out_path = dataset_cards_dir / f"{target}.md"
    out_path.write_text(rendered, encoding="utf-8")
    return str(out_path)
