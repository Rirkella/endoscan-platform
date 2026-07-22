"""Retrieve endpoint labels from approved label sources.

Normalizes each source's native call into a binary ``label`` plus an assay source
and a confidence, and records — but never silently resolves — conflicting labels
by the documented label policy. Final exclusion of conflicted compounds happens in
`candidate_table_builder` once compounds are harmonized to a canonical id.

Each label source has its own native columns, so parsing is dispatched by
``source.id``. The fixture shapes these parsers expect are documented in
``tests/fixtures/datasets/README.md`` and are flagged stand-ins for the real
source schemas.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import RawTable, SourceAdapter
from .sources import SourceEntry, SourcesAllowList, require_allowed

# Source-level confidence priors for binary HTS calls that carry no per-row
# confidence. These are documented conventions (assay reliability priors), NOT
# measured values; CERAPP/CoMPARA use their consensus score instead.
_TOXCAST_CONFIDENCE = 0.9
_TOX21_CONFIDENCE = 0.8


class LabelRecord(BaseModel):
    """One (compound, target) label from one assay/source."""

    model_config = ConfigDict(extra="forbid")

    compound_id: str
    compound_id_type: str
    target: str
    label: int  # 0 = inactive, 1 = active
    assay_source: str  # source id
    assay_name: str | None = None
    confidence: float
    raw_call: str | None = None


class LabelConflict(BaseModel):
    """A compound whose label disagrees across records (same raw id space)."""

    model_config = ConfigDict(extra="forbid")

    compound_id: str
    compound_id_type: str
    target: str
    labels: list[int]
    assay_sources: list[str]


class LabelSet(BaseModel):
    """All label records plus recorded (unresolved) conflicts."""

    model_config = ConfigDict(extra="forbid")

    records: list[LabelRecord] = Field(default_factory=list)
    conflicts: list[LabelConflict] = Field(default_factory=list)


def _parse_toxcast(rows: RawTable, target: str) -> list[LabelRecord]:
    records: list[LabelRecord] = []
    for row in rows:
        if row.get("target") != target or row.get("hitcall") is None:
            continue
        records.append(
            LabelRecord(
                compound_id=str(row["casrn"]),
                compound_id_type="CASRN",
                target=target,
                label=int(row["hitcall"]),
                assay_source="toxcast",
                assay_name=row.get("assay_name"),
                confidence=_TOXCAST_CONFIDENCE,
                raw_call=str(row["hitcall"]),
            )
        )
    return records


def _parse_tox21(rows: RawTable, target: str) -> list[LabelRecord]:
    mapping = {"active": 1, "inactive": 0}
    records: list[LabelRecord] = []
    for row in rows:
        if row.get("target") != target:
            continue
        activity = row.get("activity")
        if activity not in mapping:  # drop "inconclusive" / missing
            continue
        records.append(
            LabelRecord(
                compound_id=str(row["casrn"]),
                compound_id_type="CASRN",
                target=target,
                label=mapping[activity],
                assay_source="tox21",
                assay_name=row.get("assay_name"),
                confidence=_TOX21_CONFIDENCE,
                raw_call=str(activity),
            )
        )
    return records


def _parse_consensus(rows: RawTable, target: str, source_id: str) -> list[LabelRecord]:
    """Parser shared by CERAPP (ER) and CoMPARA (AR) consensus tables."""
    mapping = {"active": 1, "inactive": 0}
    records: list[LabelRecord] = []
    for row in rows:
        if row.get("target") != target:
            continue
        call = row.get("consensus_call")
        if call not in mapping:
            continue
        score = row.get("consensus_score")
        records.append(
            LabelRecord(
                compound_id=str(row["casrn"]),
                compound_id_type="CASRN",
                target=target,
                label=mapping[call],
                assay_source=source_id,
                assay_name=None,
                confidence=float(score) if score is not None else 0.5,
                raw_call=str(call),
            )
        )
    return records


def _parse_source(source: SourceEntry, rows: RawTable, target: str) -> list[LabelRecord]:
    if source.id == "toxcast":
        return _parse_toxcast(rows, target)
    if source.id == "tox21":
        return _parse_tox21(rows, target)
    if source.id in ("cerapp", "compara"):
        return _parse_consensus(rows, target, source.id)
    raise NotImplementedError(
        f"No label parser for source {source.id!r}; add one when the source is approved."
    )


def label_retriever(
    target: str,
    sources: list[SourceEntry],
    adapter: SourceAdapter,
    allow_list: SourcesAllowList | None = None,
) -> LabelSet:
    """Collect and normalize labels for ``target`` from the given label sources.

    When ``allow_list`` is provided, every source is checked against it first.
    Conflicting labels (same raw compound id, differing label) are recorded in
    ``conflicts`` and kept in ``records`` — never silently resolved.
    """
    if allow_list is not None:
        require_allowed(sources, allow_list)

    records: list[LabelRecord] = []
    for source in sources:
        if source.type != "labels":
            raise ValueError(f"Source {source.id!r} is not a labels source")
        records.extend(_parse_source(source, adapter.read_records(source), target))

    conflicts = _detect_conflicts(records)
    return LabelSet(records=records, conflicts=conflicts)


def _detect_conflicts(records: list[LabelRecord]) -> list[LabelConflict]:
    grouped: dict[tuple[str, str, str], list[LabelRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.compound_id, record.compound_id_type, record.target)].append(record)

    conflicts: list[LabelConflict] = []
    for (compound_id, id_type, target), group in grouped.items():
        labels = sorted({record.label for record in group})
        if len(labels) > 1:
            conflicts.append(
                LabelConflict(
                    compound_id=compound_id,
                    compound_id_type=id_type,
                    target=target,
                    labels=labels,
                    assay_sources=sorted({record.assay_source for record in group}),
                )
            )
    return conflicts
