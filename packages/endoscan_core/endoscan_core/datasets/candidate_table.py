"""Build the candidate training table with a compound-level split plan.

Produces ``X`` (transcriptomic features), ``y`` (labels), ``metadata``, and a
compound-level ``SplitPlan``. Each *signature* is a row; a "compound" is keyed by
canonical InChIKey. Conflicted-label compounds are excluded from ``y`` (recorded,
never voted, as required by the documented label policy).

The split plan is a GROUP ASSIGNMENT (compound -> group), not a trained split:
every row inherits its compound's group, so no compound can span groups. M3 feeds
these groups to a grouped CV splitter; M2 does not train.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .compound_mapper import MappingResult
from .label_retriever import LabelSet
from .overlap import OverlapResult, resolve_canonical_labels
from .signature_retriever import SignatureSet


class SplitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "compound_level"
    n_groups: int
    seed: int
    groups: dict[str, int] = Field(default_factory=dict)  # InChIKey -> group id
    folds: dict[int, list[str]] = Field(default_factory=dict)  # group id -> InChIKeys


@dataclass
class CandidateTable:
    X: pd.DataFrame
    y: pd.Series
    metadata: pd.DataFrame
    split_plan: SplitPlan
    feature_names: list[str]
    excluded_conflicts: list[str] = field(default_factory=list)


def _assign_groups(labels_by_key: dict[str, int], n_groups: int, seed: int) -> dict[str, int]:
    """Class-stratified, deterministic round-robin of compounds into groups.

    Sorting by key makes assignment reproducible; ``seed`` rotates the starting
    group per class. Keeping the round-robin within each class balances classes
    across groups, which helps a leakage-free split stay feasible.
    """
    groups: dict[str, int] = {}
    by_class: dict[int, list[str]] = {}
    for key, label in labels_by_key.items():
        by_class.setdefault(label, []).append(key)

    for keys in by_class.values():
        for offset, key in enumerate(sorted(keys)):
            groups[key] = (offset + seed) % n_groups
    return groups


def candidate_table_builder(
    labels: LabelSet,
    signatures: SignatureSet,
    overlap: OverlapResult,
    mapping: MappingResult,
    n_groups: int = 5,
    seed: int = 0,
) -> CandidateTable:
    """Assemble X/y/metadata + a compound-level split plan from the overlap."""
    labels_by_key, conflicted_keys = resolve_canonical_labels(labels, mapping)
    overlap_set = set(overlap.compound_keys)

    # Usable compounds: in the overlap, non-conflicted, with a resolved label.
    usable_keys = {k for k in overlap_set if k in labels_by_key and k not in conflicted_keys}
    excluded = sorted(k for k in overlap_set if k in conflicted_keys)

    usable_labels = {k: labels_by_key[k] for k in usable_keys}
    groups = _assign_groups(usable_labels, n_groups=n_groups, seed=seed)

    feature_names = list(signatures.feature_names)
    feature_rows: list[dict[str, float]] = []
    label_values: list[int] = []
    meta_rows: list[dict[str, object]] = []

    for record in signatures.records:
        key = mapping.to_canonical(record.compound_id, record.compound_id_type)
        if key is None or key not in usable_keys:
            continue
        feature_rows.append({gene: record.features[gene] for gene in feature_names})
        label_values.append(usable_labels[key])
        meta_rows.append(
            {
                "compound_key": key,
                "inchikey_block1": key[:14],
                "perturbagen_id": record.perturbagen_id,
                "cell_line": record.metadata.cell_line,
                "dose": record.metadata.dose,
                "time": record.metadata.time,
                "split_group": groups[key],
                "label_conflict": False,
            }
        )

    X = pd.DataFrame(feature_rows, columns=feature_names)
    y = pd.Series(label_values, name="label", dtype="int64")
    metadata = pd.DataFrame(meta_rows)

    folds: dict[int, list[str]] = {}
    for key, group in groups.items():
        folds.setdefault(group, []).append(key)
    for group in folds:
        folds[group].sort()

    split_plan = SplitPlan(n_groups=n_groups, seed=seed, groups=groups, folds=folds)
    return CandidateTable(
        X=X,
        y=y,
        metadata=metadata,
        split_plan=split_plan,
        feature_names=feature_names,
        excluded_conflicts=excluded,
    )
