"""Retrieve transcriptomic signatures from approved signature sources (LINCS).

Each signature is a perturbagen-level differential-expression vector over landmark
genes plus assay metadata (cell line, dose, time). Features are transcriptomic
only — no chemical structure is used to produce them.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import RawTable, SourceAdapter
from .sources import SourceEntry, SourcesAllowList, require_allowed

# Non-feature (metadata) columns in a LINCS signature fixture row; every other
# column is treated as a landmark-gene feature. ``compound_id``/``compound_key`` are
# the ID columns of a compound-level FUSED matrix and are never numeric features.
_LINCS_META_COLUMNS = {
    "sig_id",
    "pert_id",
    "pert_iname",
    "cell_id",
    "pert_dose",
    "pert_dose_unit",
    "pert_time",
    "pert_time_unit",
    "compound_id",
    "compound_key",
}

# Columns that identify a compound in a fused (compound-level) matrix, in priority
# order; ``compound_key`` is accepted as a back-compat alias of ``compound_id`` so the
# already-staged ``lincs.parquet`` (written with ``compound_key``) loads unchanged.
_FUSED_ID_COLUMNS = ("compound_id", "compound_key")

# Columns whose presence marks a row as SIGNATURE-level (per-signature LINCS); if any
# is present we take the signature path, so existing fixtures are byte-for-byte unchanged.
_SIGNATURE_MARKERS = ("sig_id", "pert_id", "cell_id")


class SignatureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_line: str | None = None
    dose: float | None = None
    dose_unit: str | None = None
    time: float | None = None
    time_unit: str | None = None


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature_id: str
    perturbagen_id: str
    compound_id: str
    compound_id_type: str
    features: dict[str, float]
    metadata: SignatureMetadata


class SignatureSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[SignatureRecord] = Field(default_factory=list)
    feature_names: list[str] = Field(default_factory=list)
    # "signature" = per-signature LINCS rows; "compound" = a compound-level fused
    # matrix (one mean-fused vector per compound). Drives metadata-coverage and
    # duplicate-rate semantics in the quality report without weakening either path.
    granularity: Literal["signature", "compound"] = "signature"


def _fused_id_column(row: dict) -> str | None:
    """Return the fused ID column present in ``row`` (compound_id|compound_key), or None."""
    for column in _FUSED_ID_COLUMNS:
        if column in row:
            return column
    return None


def _is_fused(rows: RawTable) -> bool:
    """A fused (compound-level) matrix has an ID column and NO per-signature markers."""
    if not rows:
        return False
    first = rows[0]
    if any(marker in first for marker in _SIGNATURE_MARKERS):
        return False  # signature-level takes priority -> existing path, unchanged
    return _fused_id_column(first) is not None


def _feature_columns(rows: RawTable) -> list[str]:
    if not rows:
        return []
    return [column for column in rows[0] if column not in _LINCS_META_COLUMNS]


def _parse_lincs(rows: RawTable, feature_names: list[str]) -> list[SignatureRecord]:
    records: list[SignatureRecord] = []
    for row in rows:
        features = {gene: float(row[gene]) for gene in feature_names}
        records.append(
            SignatureRecord(
                signature_id=str(row["sig_id"]),
                perturbagen_id=str(row["pert_id"]),
                compound_id=str(row["pert_id"]),
                compound_id_type="PERT_ID",
                features=features,
                metadata=SignatureMetadata(
                    cell_line=row.get("cell_id"),
                    dose=_as_float(row.get("pert_dose")),
                    dose_unit=row.get("pert_dose_unit"),
                    time=_as_float(row.get("pert_time")),
                    time_unit=row.get("pert_time_unit"),
                ),
            )
        )
    return records


def _parse_fused(rows: RawTable, feature_names: list[str]) -> list[SignatureRecord]:
    """Parse a compound-level FUSED matrix: one record per row, keyed by InChIKey.

    The ID column (``compound_id`` or its ``compound_key`` alias) carries a full
    InChIKey — already the canonical id — so ``compound_id_type="inchikey"`` and the
    per-signature metadata (cell/dose/time, consumed by fusion) is intentionally empty.
    """
    records: list[SignatureRecord] = []
    for row in rows:
        id_col = _fused_id_column(row)
        compound_id = str(row[id_col])
        features = {gene: float(row[gene]) for gene in feature_names}
        records.append(
            SignatureRecord(
                signature_id=compound_id,
                perturbagen_id=compound_id,
                compound_id=compound_id,
                compound_id_type="inchikey",
                features=features,
                metadata=SignatureMetadata(),
            )
        )
    return records


def _as_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def signature_retriever(
    sources: list[SourceEntry],
    adapter: SourceAdapter,
    compounds: Collection[str] | None = None,
    allow_list: SourcesAllowList | None = None,
) -> SignatureSet:
    """Collect signatures from the given signature sources.

    ``compounds`` (optional) restricts results to those perturbagen ids. When
    ``allow_list`` is given, sources are checked against it first.
    """
    if allow_list is not None:
        require_allowed(sources, allow_list)

    all_records: list[SignatureRecord] = []
    feature_names: list[str] = []
    granularity: Literal["signature", "compound"] = "signature"
    for source in sources:
        if source.type != "signatures":
            raise ValueError(f"Source {source.id!r} is not a signatures source")
        rows = adapter.read_records(source)
        names = _feature_columns(rows)
        if not feature_names:
            feature_names = names
        elif names and names != feature_names:
            raise ValueError(
                f"Inconsistent feature columns across signature sources: "
                f"{feature_names} vs {names}"
            )
        if _is_fused(rows):
            granularity = "compound"
            all_records.extend(_parse_fused(rows, feature_names))
        else:
            all_records.extend(_parse_lincs(rows, feature_names))

    if compounds is not None:
        wanted = set(compounds)
        all_records = [r for r in all_records if r.perturbagen_id in wanted]

    return SignatureSet(records=all_records, feature_names=feature_names, granularity=granularity)
