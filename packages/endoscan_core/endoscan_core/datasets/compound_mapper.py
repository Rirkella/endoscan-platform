"""Harmonize heterogeneous compound identifiers to a single canonical id.

Canonical id = the FULL InChIKey (27 characters). Rationale: it is structure-
derived and database-independent, and — unlike the 14-character first block — it
never merges distinct structures (salts/stereoisomers stay separate). This is the
M2 default policy (flagged for review); the first block is recorded in
``inchikey_block1`` so a collapse policy can be revisited later without re-mapping.

SMILES is carried here only as an identifier/provenance field. It is NEVER used to
predict a signature or risk (see ``docs/SCIENTIFIC_LIMITATIONS.md``).
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import RawTable, SourceAdapter
from .sources import SourceEntry, SourcesAllowList, require_allowed

MappingConfidence = str  # "exact" | "ambiguous" | "unmapped"

# Id types that are ALREADY the canonical id (a full InChIKey). These need no
# mapping-source lookup: a fused compound-level signature is keyed directly by its
# InChIKey, so it maps to itself (identity). CASRN/PERT_ID still go through the source.
_CANONICAL_ID_TYPES = {"inchikey"}


def _normalize_inchikey(value: str) -> str:
    """Strip + upper-case an InChIKey for like-for-like identity (no truncation)."""
    return str(value).strip().upper()


def _is_canonical_id_type(id_type: str) -> bool:
    return str(id_type).strip().lower() in _CANONICAL_ID_TYPES


class CompoundMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str
    input_id_type: str
    canonical_id: str | None  # full InChIKey
    inchikey_block1: str | None  # first 14 chars (recorded for future collapse policy)
    cid: str | None = None
    smiles: str | None = None
    confidence: MappingConfidence


class MappingConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str
    input_id_type: str
    canonical_ids: list[str]


class MappingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[CompoundMapping] = Field(default_factory=list)
    unmapped: list[CompoundMapping] = Field(default_factory=list)
    conflicts: list[MappingConflict] = Field(default_factory=list)

    def to_canonical(self, input_id: str, input_id_type: str) -> str | None:
        """Return the canonical InChIKey for an input id, or None if not exactly mapped.

        An id whose type is already canonical (InChIKey) maps to itself — no
        mapping-source row is required for the fused compound-level path.
        """
        if _is_canonical_id_type(input_id_type):
            return _normalize_inchikey(input_id)
        for mapping in self.mappings:
            if (
                mapping.input_id == input_id
                and mapping.input_id_type == input_id_type
                and mapping.confidence == "exact"
            ):
                return mapping.canonical_id
        return None


def _index_mapping_rows(rows: RawTable) -> dict[tuple[str, str], list[dict]]:
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (str(row["input_id"]), str(row["input_id_type"]))
        index[key].append(row)
    return index


def compound_mapper(
    ids: list[tuple[str, str]],
    mapping_source: SourceEntry,
    adapter: SourceAdapter,
    allow_list: SourcesAllowList | None = None,
) -> MappingResult:
    """Map ``(raw_id, id_type)`` pairs to canonical InChIKeys via a mapping source.

    - exactly one InChIKey  -> ``confidence="exact"`` (usable downstream)
    - multiple InChIKeys     -> recorded in ``conflicts``, ``confidence="ambiguous"``,
      canonical left ``None`` (never silently resolved)
    - no match               -> recorded in ``unmapped``, ``confidence="unmapped"``
    """
    if allow_list is not None:
        require_allowed([mapping_source], allow_list)
    if mapping_source.type != "mapping":
        raise ValueError(f"Source {mapping_source.id!r} is not a mapping source")

    index = _index_mapping_rows(adapter.read_records(mapping_source))

    result = MappingResult()
    for raw_id, id_type in _dedupe(ids):
        if _is_canonical_id_type(id_type):  # already a canonical InChIKey -> identity
            inchikey = _normalize_inchikey(raw_id)
            result.mappings.append(
                CompoundMapping(
                    input_id=raw_id,
                    input_id_type=id_type,
                    canonical_id=inchikey,
                    inchikey_block1=inchikey[:14],
                    confidence="exact",
                )
            )
            continue
        rows = index.get((raw_id, id_type), [])
        inchikeys = sorted({str(row["inchikey"]) for row in rows if row.get("inchikey")})

        if not inchikeys:
            result.unmapped.append(
                CompoundMapping(
                    input_id=raw_id,
                    input_id_type=id_type,
                    canonical_id=None,
                    inchikey_block1=None,
                    confidence="unmapped",
                )
            )
            continue

        if len(inchikeys) > 1:
            result.conflicts.append(
                MappingConflict(input_id=raw_id, input_id_type=id_type, canonical_ids=inchikeys)
            )
            result.mappings.append(
                CompoundMapping(
                    input_id=raw_id,
                    input_id_type=id_type,
                    canonical_id=None,
                    inchikey_block1=None,
                    confidence="ambiguous",
                )
            )
            continue

        inchikey = inchikeys[0]
        row = rows[0]
        result.mappings.append(
            CompoundMapping(
                input_id=raw_id,
                input_id_type=id_type,
                canonical_id=inchikey,
                inchikey_block1=inchikey[:14],
                cid=_as_str(row.get("cid")),
                smiles=_as_str(row.get("smiles")),
                confidence="exact",
            )
        )
    return result


def _dedupe(ids: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for pair in ids:
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return ordered


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)
