"""Retrieve transcriptomic signatures from approved signature sources (LINCS).

Each signature is a perturbagen-level differential-expression vector over landmark
genes plus assay metadata (cell line, dose, time). Features are transcriptomic
only — no chemical structure is used to produce them.
"""

from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import RawTable, SourceAdapter
from .sources import SourceEntry, SourcesAllowList, require_allowed

# Non-feature (metadata) columns in a LINCS signature fixture row; every other
# column is treated as a landmark-gene feature.
_LINCS_META_COLUMNS = {
    "sig_id",
    "pert_id",
    "pert_iname",
    "cell_id",
    "pert_dose",
    "pert_dose_unit",
    "pert_time",
    "pert_time_unit",
}


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
        all_records.extend(_parse_lincs(rows, feature_names))

    if compounds is not None:
        wanted = set(compounds)
        all_records = [r for r in all_records if r.perturbagen_id in wanted]

    return SignatureSet(records=all_records, feature_names=feature_names)
