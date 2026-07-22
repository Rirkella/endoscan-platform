"""Compute the compound-level overlap of labels and signatures.

A "compound" is keyed by its canonical InChIKey (from `compound_mapper`). Overlap
is the set of compounds that have both a (non-conflicted) label for the target and
at least one transcriptomic signature.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .compound_mapper import MappingResult
from .label_retriever import LabelSet
from .signature_retriever import SignatureSet


class OverlapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compound_keys: list[str] = Field(default_factory=list)  # InChIKeys in BOTH
    n_labeled: int = 0
    n_with_signature: int = 0
    n_overlap: int = 0
    per_class_overlap: dict[int, int] = Field(default_factory=dict)


def resolve_canonical_labels(
    labels: LabelSet, mapping: MappingResult
) -> tuple[dict[str, int], set[str]]:
    """Resolve labels to canonical-compound level.

    Returns ``(labels_by_key, conflicted_keys)`` where ``labels_by_key`` maps each
    canonical InChIKey with a single agreed label to that label, and
    ``conflicted_keys`` is the set of InChIKeys whose labels disagree across
    records (these are recorded and excluded, never voted on by aggregation).
    Records that cannot be mapped to a canonical id are skipped here (they are
    counted as unmapped by the mapper).
    """
    by_key: dict[str, set[int]] = {}
    for record in labels.records:
        key = mapping.to_canonical(record.compound_id, record.compound_id_type)
        if key is None:
            continue
        by_key.setdefault(key, set()).add(record.label)

    labels_by_key: dict[str, int] = {}
    conflicted_keys: set[str] = set()
    for key, values in by_key.items():
        if len(values) == 1:
            labels_by_key[key] = next(iter(values))
        else:
            conflicted_keys.add(key)
    return labels_by_key, conflicted_keys


def signature_keys(signatures: SignatureSet, mapping: MappingResult) -> set[str]:
    """Canonical InChIKeys that have at least one signature."""
    keys: set[str] = set()
    for record in signatures.records:
        key = mapping.to_canonical(record.compound_id, record.compound_id_type)
        if key is not None:
            keys.add(key)
    return keys


def overlap_computer(
    labels: LabelSet,
    signatures: SignatureSet,
    mapping: MappingResult,
) -> OverlapResult:
    """Compound-level intersection of labels and signatures, with per-class counts."""
    labels_by_key, conflicted_keys = resolve_canonical_labels(labels, mapping)
    labeled_keys = set(labels_by_key) | conflicted_keys
    sig_keys = signature_keys(signatures, mapping)

    overlap_keys = sorted(labeled_keys & sig_keys)

    per_class: dict[int, int] = {}
    for key in overlap_keys:
        if key in labels_by_key:  # exclude conflicted from per-class counts
            label = labels_by_key[key]
            per_class[label] = per_class.get(label, 0) + 1

    return OverlapResult(
        compound_keys=overlap_keys,
        n_labeled=len(labeled_keys),
        n_with_signature=len(sig_keys),
        n_overlap=len(overlap_keys),
        per_class_overlap=per_class,
    )
