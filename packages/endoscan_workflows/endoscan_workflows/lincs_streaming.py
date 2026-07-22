"""Streaming LINCS metadata normalization and disk-backed exact coverage.

The production path deliberately keeps full LINCS rows out of Pydantic response models,
agent context, workflow JSON, and the source-response cache.  Raw reviewed gzip artifacts
are streamed into deterministic, per-role SQLite artifacts whose indexes are queried lazily.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .artifacts import LocalArtifactStore
from .contracts import ArtifactDescriptor, ToolInvocation
from .discovery_strategy import (
    ArtifactReference,
    ImmutableV2Contract,
    deterministic_fingerprint,
)
from .lincs_metadata import (
    MAXIMUM_DECOMPRESSED_METADATA_BYTES,
    METADATA_ROLES,
    CompoundIdentityRecord,
    LincsCompoundMatch,
    LincsCompoundMatchStatus,
    LincsContextFilter,
    LincsMetadataProvider,
    LincsMetadataRetrievalInput,
    LincsReleaseManifest,
    LincsReleaseRegistry,
    LincsReleaseResource,
    MetadataRole,
)
from .provider_execution import (
    ProviderExecutionOutcome,
    ProviderResourceRequest,
    ProviderRetrievalTask,
    ProviderTaskExecutor,
)

LINCS_NORMALIZER_VERSION: Literal["2.0.0"] = "2.0.0"
LINCS_NORMALIZED_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
LINCS_INDEX_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
LINCS_COVERAGE_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
FULL_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
NORMALIZATION_BATCH_SIZE = 5_000
TOOL_PREVIEW_LIMIT = 100


def _artifact_reference(
    descriptor: ArtifactDescriptor, artifact_type: str | None = None
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=descriptor.id,
        sha256=descriptor.sha256,
        artifact_type=artifact_type or descriptor.artifact_type,
    )


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if not text or text.casefold() in {"-666", "na", "nan", "none", "null"} else text


def _mapped(row: dict[str, str], mapping: dict[str, str], field: str) -> str | None:
    source_field = mapping.get(field)
    return _clean(row.get(source_field, "")) if source_field else None


def _inchikey(value: str | None) -> str | None:
    normalized = (value or "").upper()
    return normalized if FULL_INCHIKEY.fullmatch(normalized) else None


def _cid(value: str | None) -> str | None:
    normalized = (value or "").removeprefix("CID:")
    return f"CID:{normalized}" if re.fullmatch(r"[1-9][0-9]{0,11}", normalized) else None


def _bool(value: str | None) -> int | None:
    normalized = (value or "").casefold()
    if normalized in {"1", "true", "yes", "y"}:
        return 1
    if normalized in {"0", "false", "no", "n"}:
        return 0
    return None


def _rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def parse_lincs_metadata_resource_compact(
    resource: ProviderResourceRequest, content: bytes
) -> dict[str, Any]:
    """Validate and count one reviewed resource without retaining its rows."""

    if resource.logical_role == "level5_gctx":
        raise ValueError("Level-5 GCTX access is prohibited during metadata discovery")
    try:
        binary = (
            gzip.GzipFile(fileobj=io.BytesIO(content))
            if resource.compression == "gzip"
            else io.BytesIO(content)
        )
        with binary, io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text, delimiter="\t")
            if reader.fieldnames is None:
                raise ValueError("LINCS metadata resource has no header")
            columns = list(reader.fieldnames)
            record_count = 0
            for _ in reader:
                record_count += 1
                if binary.tell() > MAXIMUM_DECOMPRESSED_METADATA_BYTES:
                    raise ValueError("LINCS metadata exceeded decompressed size policy")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise ValueError("LINCS metadata resource is not valid reviewed gzip content") from exc
    return {"columns": columns, "record_count": record_count, "rows_materialized": False}


class LincsRoleNormalizationManifest(ImmutableV2Contract):
    logical_role: MetadataRole
    raw_artifact: ArtifactReference
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized_artifact: ArtifactReference
    normalized_format: Literal["sqlite"] = "sqlite"
    source_release: str
    source_locator: str
    normalizer_version: Literal["2.0.0"] = LINCS_NORMALIZER_VERSION
    normalized_schema_version: Literal["2.0.0"] = LINCS_NORMALIZED_SCHEMA_VERSION
    row_count: int = Field(ge=0)
    included_row_count: int = Field(ge=0)
    incomplete_row_count: int = Field(ge=0)
    excluded_row_count: int = Field(ge=0)
    normalized_columns: list[str]
    source_columns: list[str]
    source_field_mapping: dict[str, str]
    source_field_mapping_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    exclusion_reason_counts: dict[str, int]
    normalized_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized_size_bytes: int = Field(ge=0)
    normalization_wall_time_ms: int = Field(ge=0)
    indexing_wall_time_ms: int = Field(ge=0)
    created_at: datetime
    provenance: list[str]


class LincsDiskIndexRole(ImmutableV2Contract):
    logical_role: MetadataRole
    normalized_artifact: ArtifactReference
    sql_indexes: list[str]
    row_count: int = Field(ge=0)


class LincsDiskIndexManifest(ImmutableV2Contract):
    release_id: str
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    index_schema_version: Literal["2.0.0"] = LINCS_INDEX_SCHEMA_VERSION
    roles: list[LincsDiskIndexRole]
    total_indexed_rows: int = Field(ge=0)
    total_artifact_bytes: int = Field(ge=0)
    index_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class LincsStreamingNormalizationManifest(ImmutableV2Contract):
    provider: Literal["lincs-l1000"] = "lincs-l1000"
    release_id: str
    source_release: str
    source_locator: str
    release_manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalizer_version: Literal["2.0.0"] = LINCS_NORMALIZER_VERSION
    normalized_schema_version: Literal["2.0.0"] = LINCS_NORMALIZED_SCHEMA_VERSION
    cache_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    role_artifacts: list[LincsRoleNormalizationManifest]
    role_manifest_artifacts: dict[MetadataRole, ArtifactReference]
    index_manifest_artifact: ArtifactReference
    index_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_normalization_wall_time_ms: int = Field(ge=0)
    total_indexing_wall_time_ms: int = Field(ge=0)
    peak_resident_memory_bytes: int | None = Field(default=None, ge=0)
    expression_values_retrieved: Literal[False] = False

    def role(self, logical_role: MetadataRole) -> LincsRoleNormalizationManifest:
        matches = [item for item in self.role_artifacts if item.logical_role == logical_role]
        if len(matches) != 1:
            raise KeyError(logical_role)
        return matches[0]


class LincsStreamingRetrievalOutput(ImmutableV2Contract):
    execution: ProviderExecutionOutcome
    normalization_manifest: LincsStreamingNormalizationManifest | None = None
    normalization_manifest_artifact: ArtifactReference | None = None
    normalization_ran: bool = False
    indexing_ran: bool = False


class LincsDiskIndexInput(ImmutableV2Contract):
    normalization_manifest_artifact: ArtifactReference


class LincsDiskIndexOutput(ImmutableV2Contract):
    index_manifest_artifact: ArtifactReference
    index_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_indexed_rows: int = Field(ge=0)
    total_artifact_bytes: int = Field(ge=0)
    index_rebuilt: Literal[False] = False


class LincsCoverageContextSummary(ImmutableV2Contract):
    view_id: str
    operator: Literal["individual", "union", "intersection", "at_least_one", "every"]
    context_ids: list[str]
    compound_count: int = Field(ge=0)
    signature_count: int = Field(ge=0)
    compound_ids_preview: list[str] = Field(default_factory=list, max_length=TOOL_PREVIEW_LIMIT)
    exact_sig_ids_preview: list[str] = Field(default_factory=list, max_length=TOOL_PREVIEW_LIMIT)


class LincsDiskCoverageInput(ImmutableV2Contract):
    compounds: list[CompoundIdentityRecord]
    normalization_manifest_artifact: ArtifactReference
    contexts: list[LincsContextFilter] = Field(default_factory=list)
    preview_limit: int = Field(default=20, ge=0, le=TOOL_PREVIEW_LIMIT)


class LincsDiskCoverageOutput(ImmutableV2Contract):
    release_id: str
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_artifact: ArtifactReference
    total_input_compounds: int = Field(ge=0)
    exact_perturbagen_matches: int = Field(ge=0)
    ambiguous_matches: int = Field(ge=0)
    unmatched_compounds: int = Field(ge=0)
    compounds_with_measured_chemical_signatures: int = Field(ge=0)
    exact_available_signature_count: int = Field(ge=0)
    compound_matches_preview: list[LincsCompoundMatch] = Field(
        default_factory=list, max_length=TOOL_PREVIEW_LIMIT
    )
    exact_sig_ids_preview: list[str] = Field(default_factory=list, max_length=TOOL_PREVIEW_LIMIT)
    context_views: list[LincsCoverageContextSummary]
    aggregate_distinct_value_counts: dict[str, int]
    aggregate_previews: dict[str, dict[str, int]]
    missing_metadata_fields: dict[str, int]
    exclusion_reasons: dict[str, int]
    coverage_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    complete_result_in_artifact: Literal[True] = True
    expression_values_retrieved: Literal[False] = False


ROLE_COLUMNS: dict[MetadataRole, list[str]] = {
    "pert_info": [
        "record_id",
        "source_row_number",
        "pert_id",
        "perturbation_type",
        "canonical_name",
        "inchikey",
        "pubchem_cid",
        "canonical_smiles",
        "row_status",
        "exclusion_reason",
        "source_release",
    ],
    "sig_info": [
        "record_id",
        "source_row_number",
        "sig_id",
        "pert_id",
        "perturbation_type",
        "cell_id",
        "dose",
        "dose_unit",
        "exposure_time",
        "time_unit",
        "quality_status",
        "processing_metadata",
        "measured_chemical_perturbation",
        "row_status",
        "exclusion_reason",
        "source_release",
    ],
    "cell_info": [
        "record_id",
        "source_row_number",
        "cell_id",
        "organism",
        "tissue_or_lineage",
        "disease_or_context",
        "row_status",
        "exclusion_reason",
        "source_release",
    ],
    "gene_info": [
        "record_id",
        "source_row_number",
        "source_gene_identifier",
        "gene_symbol",
        "landmark_gene_status",
        "row_status",
        "exclusion_reason",
        "source_release",
    ],
}


ROLE_INDEXES: dict[MetadataRole, list[str]] = {
    "pert_info": [
        "idx_perturbagens_pert_id",
        "idx_perturbagens_inchikey",
        "idx_perturbagens_pubchem_cid",
        "idx_perturbagen_names_exact",
    ],
    "sig_info": [
        "idx_signatures_pert_id",
        "idx_signatures_sig_id",
        "idx_signatures_context",
    ],
    "cell_info": ["idx_cells_cell_id"],
    "gene_info": ["idx_genes_identifier", "idx_genes_landmark"],
}


def _create_role_schema(connection: sqlite3.Connection, role: MetadataRole) -> None:
    if role == "pert_info":
        connection.executescript(
            """
            CREATE TABLE records (
              record_id TEXT PRIMARY KEY, source_row_number INTEGER NOT NULL,
              pert_id TEXT, perturbation_type TEXT, canonical_name TEXT,
              inchikey TEXT, pubchem_cid TEXT, canonical_smiles TEXT,
              row_status TEXT NOT NULL, exclusion_reason TEXT, source_release TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE exact_names (
              exact_name TEXT NOT NULL, record_id TEXT NOT NULL,
              PRIMARY KEY (exact_name, record_id)
            ) WITHOUT ROWID;
            """
        )
    elif role == "sig_info":
        connection.execute(
            """
            CREATE TABLE records (
              record_id TEXT PRIMARY KEY, source_row_number INTEGER NOT NULL,
              sig_id TEXT, pert_id TEXT, perturbation_type TEXT, cell_id TEXT,
              dose TEXT, dose_unit TEXT, exposure_time TEXT, time_unit TEXT,
              quality_status TEXT, processing_metadata TEXT,
              measured_chemical_perturbation INTEGER NOT NULL,
              row_status TEXT NOT NULL, exclusion_reason TEXT, source_release TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
    elif role == "cell_info":
        connection.execute(
            """
            CREATE TABLE records (
              record_id TEXT PRIMARY KEY, source_row_number INTEGER NOT NULL,
              cell_id TEXT, organism TEXT, tissue_or_lineage TEXT,
              disease_or_context TEXT, row_status TEXT NOT NULL,
              exclusion_reason TEXT, source_release TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
    else:
        connection.execute(
            """
            CREATE TABLE records (
              record_id TEXT PRIMARY KEY, source_row_number INTEGER NOT NULL,
              source_gene_identifier TEXT, gene_symbol TEXT,
              landmark_gene_status INTEGER, row_status TEXT NOT NULL,
              exclusion_reason TEXT, source_release TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )


def _create_role_indexes(connection: sqlite3.Connection, role: MetadataRole) -> None:
    statements = {
        "pert_info": """
          CREATE INDEX idx_perturbagens_pert_id ON records(pert_id);
          CREATE INDEX idx_perturbagens_inchikey ON records(inchikey);
          CREATE INDEX idx_perturbagens_pubchem_cid ON records(pubchem_cid);
          CREATE INDEX idx_perturbagen_names_exact ON exact_names(exact_name);
        """,
        "sig_info": """
          CREATE INDEX idx_signatures_pert_id ON records(pert_id);
          CREATE INDEX idx_signatures_sig_id ON records(sig_id);
          CREATE INDEX idx_signatures_context
            ON records(cell_id, dose, exposure_time, quality_status);
        """,
        "cell_info": "CREATE INDEX idx_cells_cell_id ON records(cell_id);",
        "gene_info": """
          CREATE INDEX idx_genes_identifier ON records(source_gene_identifier);
          CREATE INDEX idx_genes_landmark ON records(landmark_gene_status);
        """,
    }
    connection.executescript(statements[role])


def _row_values(
    role: MetadataRole,
    row_number: int,
    row: dict[str, str],
    resource: LincsReleaseResource,
    manifest: LincsReleaseManifest,
) -> tuple[tuple[Any, ...], list[str], str, str | None]:
    mapping = resource.column_mapping
    if role == "pert_info":
        pert_id = _mapped(row, mapping, "pert_id")
        pert_type = _mapped(row, mapping, "perturbation_type")
        canonical_name = _mapped(row, mapping, "canonical_name")
        status, reason = "valid", None
        if not pert_id or not pert_type:
            status, reason = "incomplete", "missing_pert_id_or_type"
        elif pert_type not in manifest.chemical_perturbation_types:
            status, reason = "excluded", "non_chemical_perturbation"
        synonyms = sorted(
            {
                value.strip()
                for field in ("pert_alias", "aliases", "synonyms")
                for value in str(row.get(field, "")).split("|")
                if value.strip()
            }
        )
        names = [value.casefold() for value in [canonical_name, *synonyms] if value]
        return (
            (
                f"{manifest.release_id}:pert:{row_number}",
                row_number,
                pert_id,
                pert_type,
                canonical_name,
                _inchikey(_mapped(row, mapping, "inchikey")),
                _cid(_mapped(row, mapping, "pubchem_cid")),
                _mapped(row, mapping, "canonical_smiles"),
                status,
                reason,
                manifest.source_release,
            ),
            names,
            status,
            reason,
        )
    if role == "sig_info":
        sig_id = _mapped(row, mapping, "sig_id")
        pert_id = _mapped(row, mapping, "pert_id")
        pert_type = _mapped(row, mapping, "perturbation_type")
        cell_id = _mapped(row, mapping, "cell_id")
        status, reason = "valid", None
        if not sig_id or not pert_id or not cell_id:
            status, reason = "incomplete", "missing_sig_pert_or_cell_identifier"
        measured = pert_type in manifest.chemical_perturbation_types
        if status == "valid" and not measured:
            status, reason = "excluded", "non_chemical_perturbation"
        return (
            (
                f"{manifest.release_id}:sig:{row_number}",
                row_number,
                sig_id,
                pert_id,
                pert_type,
                cell_id,
                _mapped(row, mapping, "dose"),
                _mapped(row, mapping, "dose_unit"),
                _mapped(row, mapping, "exposure_time"),
                _mapped(row, mapping, "time_unit"),
                _mapped(row, mapping, "quality_status"),
                _mapped(row, mapping, "processing_metadata"),
                int(measured),
                status,
                reason,
                manifest.source_release,
            ),
            [],
            status,
            reason,
        )
    if role == "cell_info":
        cell_id = _mapped(row, mapping, "cell_id")
        status, reason = ("valid", None) if cell_id else ("incomplete", "missing_cell_id")
        return (
            (
                f"{manifest.release_id}:cell:{row_number}",
                row_number,
                cell_id,
                _mapped(row, mapping, "organism"),
                _mapped(row, mapping, "tissue_or_lineage"),
                _mapped(row, mapping, "disease_or_context"),
                status,
                reason,
                manifest.source_release,
            ),
            [],
            status,
            reason,
        )
    gene_id = _mapped(row, mapping, "gene_identifier")
    symbol = _mapped(row, mapping, "gene_symbol")
    status, reason = (
        ("valid", None)
        if gene_id and symbol
        else ("incomplete", "missing_gene_identifier_or_symbol")
    )
    return (
        (
            f"{manifest.release_id}:gene:{row_number}",
            row_number,
            gene_id,
            symbol,
            _bool(_mapped(row, mapping, "landmark_gene_status")),
            status,
            reason,
            manifest.source_release,
        ),
        [],
        status,
        reason,
    )


def _insert_sql(role: MetadataRole) -> str:
    placeholders = ",".join("?" for _ in ROLE_COLUMNS[role])
    return f"INSERT INTO records ({','.join(ROLE_COLUMNS[role])}) VALUES ({placeholders})"


def _normalize_role(
    *,
    raw_path: Path,
    target_path: Path,
    role: MetadataRole,
    resource: LincsReleaseResource,
    manifest: LincsReleaseManifest,
) -> tuple[dict[str, int], list[str], int, int]:
    started = time.monotonic()
    connection = sqlite3.connect(target_path)
    counts: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    source_columns: list[str] = []
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        _create_role_schema(connection, role)
        record_batch: list[tuple[Any, ...]] = []
        name_batch: list[tuple[str, str]] = []
        with gzip.open(raw_path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None:
                raise ValueError(f"LINCS {role} has no header")
            source_columns = list(reader.fieldnames)
            missing = set(resource.expected_columns) - set(source_columns)
            if missing:
                raise ValueError(
                    f"LINCS {role} lacks reviewed columns: {', '.join(sorted(missing))}"
                )
            for row_number, source_row in enumerate(reader, start=1):
                row = {str(key): str(value or "") for key, value in source_row.items()}
                values, names, status, reason = _row_values(
                    role, row_number, row, resource, manifest
                )
                record_batch.append(values)
                if role == "pert_info":
                    name_batch.extend((name, str(values[0])) for name in names)
                counts[status] += 1
                if reason:
                    exclusions[reason] += 1
                if len(record_batch) >= NORMALIZATION_BATCH_SIZE:
                    connection.executemany(_insert_sql(role), record_batch)
                    if name_batch:
                        connection.executemany(
                            """
                            INSERT OR IGNORE INTO exact_names(exact_name, record_id)
                            VALUES (?, ?)
                            """,
                            name_batch,
                        )
                    connection.commit()
                    record_batch.clear()
                    name_batch.clear()
            if record_batch:
                connection.executemany(_insert_sql(role), record_batch)
            if name_batch:
                connection.executemany(
                    "INSERT OR IGNORE INTO exact_names(exact_name, record_id) VALUES (?, ?)",
                    name_batch,
                )
            connection.commit()
        normalization_ms = max(0, int((time.monotonic() - started) * 1000))
        index_started = time.monotonic()
        _create_role_indexes(connection, role)
        connection.commit()
        connection.execute("VACUUM")
        indexing_ms = max(0, int((time.monotonic() - index_started) * 1000))
    finally:
        connection.close()
    return (
        {
            "row_count": sum(counts.values()),
            "included": counts["valid"],
            "incomplete": counts["incomplete"],
            "excluded": counts["excluded"],
            **{f"reason:{key}": value for key, value in sorted(exclusions.items())},
        },
        source_columns,
        normalization_ms,
        indexing_ms,
    )


def lincs_normalization_cache_identity(
    manifest: LincsReleaseManifest,
    raw_hashes: dict[str, str | None],
    *,
    normalizer_version: str = LINCS_NORMALIZER_VERSION,
    normalized_schema_version: str = LINCS_NORMALIZED_SCHEMA_VERSION,
    index_schema_version: str = LINCS_INDEX_SCHEMA_VERSION,
) -> str:
    """Derive the compact cache identity without serializing normalized rows."""

    return deterministic_fingerprint(
        {
            "provider": "lincs-l1000",
            "release_id": manifest.release_id,
            "release_manifest_fingerprint": manifest.manifest_fingerprint,
            "raw_hashes": raw_hashes,
            "normalizer_version": normalizer_version,
            "normalized_schema_version": normalized_schema_version,
            "index_schema_version": index_schema_version,
            "operation": "stream_normalize_and_index",
        }
    )


def _normalization_cache_identity(
    manifest: LincsReleaseManifest, outcome: ProviderExecutionOutcome
) -> str:
    return lincs_normalization_cache_identity(
        manifest,
        {
            item.logical_role: item.observed_sha256
            for item in outcome.page_or_file_results
            if item.raw_artifact is not None
        },
    )


def lincs_bundle_fingerprint(
    manifest: LincsReleaseManifest,
    roles: list[LincsRoleNormalizationManifest],
    *,
    normalizer_version: str = LINCS_NORMALIZER_VERSION,
    normalized_schema_version: str = LINCS_NORMALIZED_SCHEMA_VERSION,
) -> str:
    """Fingerprint only compact role hashes, counts, mappings, and version inputs."""

    return deterministic_fingerprint(
        {
            "provider": "lincs-l1000",
            "release_id": manifest.release_id,
            "source_release": manifest.source_release,
            "release_manifest_fingerprint": manifest.manifest_fingerprint,
            "normalizer_version": normalizer_version,
            "normalized_schema_version": normalized_schema_version,
            "roles": [
                {
                    "logical_role": item.logical_role,
                    "raw_sha256": item.raw_sha256,
                    "normalized_sha256": item.normalized_sha256,
                    "row_count": item.row_count,
                    "included_row_count": item.included_row_count,
                    "incomplete_row_count": item.incomplete_row_count,
                    "excluded_row_count": item.excluded_row_count,
                    "source_field_mapping_sha256": item.source_field_mapping_sha256,
                }
                for item in sorted(roles, key=lambda value: value.logical_role)
            ],
        }
    )


class StreamingLincsMetadataProvider:
    """Production LINCS provider with artifact-only row persistence."""

    def __init__(self, registry: LincsReleaseRegistry, executor: ProviderTaskExecutor) -> None:
        self.registry = registry
        self.executor = executor

    def retrieval_task(
        self,
        *,
        workflow_id: str,
        task_id: str,
        release_id: str,
        idempotency_key: str,
    ) -> ProviderRetrievalTask:
        return LincsMetadataProvider(self.registry, self.executor).retrieval_task(
            workflow_id=workflow_id,
            task_id=task_id,
            release_id=release_id,
            idempotency_key=idempotency_key,
        )

    @property
    def artifacts(self) -> LocalArtifactStore:
        return self.executor.artifacts

    def _load_cached_manifest(
        self, workflow_id: str, cache_identity: str
    ) -> tuple[LincsStreamingNormalizationManifest, ArtifactReference] | None:
        logical_name = f"lincs-normalization-{cache_identity}.json"
        descriptor = self.artifacts.find_by_logical_name(workflow_id, logical_name)
        if descriptor is None:
            return None
        verified, content = self.artifacts.get(descriptor.id)
        manifest = LincsStreamingNormalizationManifest.model_validate_json(content)
        if manifest.cache_identity != cache_identity:
            raise ValueError("Cached LINCS normalization identity does not match")
        for role in manifest.role_artifacts:
            self.artifacts.verified_path(role.raw_artifact.artifact_id)
            self.artifacts.verified_path(role.normalized_artifact.artifact_id)
        for reference in manifest.role_manifest_artifacts.values():
            self.artifacts.verified_path(reference.artifact_id)
        self.artifacts.verified_path(manifest.index_manifest_artifact.artifact_id)
        return manifest, _artifact_reference(verified, "lincs_normalization_manifest")

    def _normalize_and_index(
        self,
        *,
        workflow_id: str,
        manifest: LincsReleaseManifest,
        outcome: ProviderExecutionOutcome,
        cache_identity: str,
        idempotency_key: str,
    ) -> tuple[LincsStreamingNormalizationManifest, ArtifactReference]:
        resources = {item.logical_role: item for item in manifest.resources}
        results = {
            item.logical_role: item
            for item in outcome.page_or_file_results
            if item.raw_artifact is not None
        }
        role_manifests: list[LincsRoleNormalizationManifest] = []
        peak_rss = _rss_bytes()
        with tempfile.TemporaryDirectory(
            prefix="lincs-normalize-", dir=self.artifacts.root
        ) as temp:
            temp_root = Path(temp)
            for role in METADATA_ROLES:
                result = results.get(role)
                if result is None or result.raw_artifact is None:
                    raise ValueError(f"LINCS raw resource is missing: {role}")
                raw_descriptor, raw_path = self.artifacts.verified_path(
                    result.raw_artifact.artifact_id
                )
                target = temp_root / f"lincs_{role}.sqlite"
                counts, source_columns, normalization_ms, indexing_ms = _normalize_role(
                    raw_path=raw_path,
                    target_path=target,
                    role=role,
                    resource=resources[role],
                    manifest=manifest,
                )
                normalized_descriptor = self.artifacts.put_file(
                    workflow_id=workflow_id,
                    source_path=target,
                    mime_type="application/octet-stream",
                    artifact_type=f"lincs_normalized_{role}_sqlite",
                    logical_name=f"lincs-{cache_identity}-{role}.sqlite",
                    producer="lincs-streaming-normalizer",
                    original_source=resources[role].public_locator,
                    idempotency_key=f"{idempotency_key}:normalized:{role}",
                )
                mapping_sha = deterministic_fingerprint(resources[role].column_mapping)
                role_manifests.append(
                    LincsRoleNormalizationManifest(
                        logical_role=role,
                        raw_artifact=result.raw_artifact,
                        raw_sha256=raw_descriptor.sha256,
                        normalized_artifact=_artifact_reference(normalized_descriptor),
                        source_release=manifest.source_release,
                        source_locator=resources[role].public_locator,
                        row_count=counts["row_count"],
                        included_row_count=counts["included"],
                        incomplete_row_count=counts["incomplete"],
                        excluded_row_count=counts["excluded"],
                        normalized_columns=ROLE_COLUMNS[role],
                        source_columns=source_columns,
                        source_field_mapping=resources[role].column_mapping,
                        source_field_mapping_sha256=mapping_sha,
                        exclusion_reason_counts={
                            key.removeprefix("reason:"): value
                            for key, value in counts.items()
                            if key.startswith("reason:")
                        },
                        normalized_sha256=normalized_descriptor.sha256,
                        normalized_size_bytes=normalized_descriptor.size_bytes,
                        normalization_wall_time_ms=normalization_ms,
                        indexing_wall_time_ms=indexing_ms,
                        created_at=normalized_descriptor.created_at,
                        provenance=[
                            *manifest.licence_and_provenance,
                            *resources[role].licence_and_provenance,
                        ],
                    )
                )
                observed_rss = _rss_bytes()
                peak_rss = max(
                    (value for value in (peak_rss, observed_rss) if value is not None),
                    default=None,
                )

        bundle_fingerprint = lincs_bundle_fingerprint(manifest, role_manifests)
        index_roles = [
            LincsDiskIndexRole(
                logical_role=item.logical_role,
                normalized_artifact=item.normalized_artifact,
                sql_indexes=ROLE_INDEXES[item.logical_role],
                row_count=item.row_count,
            )
            for item in role_manifests
        ]
        index_fingerprint = deterministic_fingerprint(
            {
                "release_id": manifest.release_id,
                "bundle_fingerprint": bundle_fingerprint,
                "index_schema_version": LINCS_INDEX_SCHEMA_VERSION,
                "roles": [
                    {
                        "logical_role": item.logical_role,
                        "normalized_sha256": next(
                            role.normalized_sha256
                            for role in role_manifests
                            if role.logical_role == item.logical_role
                        ),
                        "sql_indexes": item.sql_indexes,
                        "row_count": item.row_count,
                    }
                    for item in index_roles
                ],
            }
        )
        index_manifest = LincsDiskIndexManifest(
            release_id=manifest.release_id,
            bundle_fingerprint=bundle_fingerprint,
            roles=index_roles,
            total_indexed_rows=sum(item.row_count for item in index_roles),
            total_artifact_bytes=sum(item.normalized_size_bytes for item in role_manifests),
            index_fingerprint=index_fingerprint,
        )
        index_descriptor = self.artifacts.put_json(
            workflow_id=workflow_id,
            value=index_manifest.model_dump(mode="json"),
            artifact_type="lincs_disk_index_manifest",
            logical_name=f"lincs-index-{cache_identity}.json",
            producer="lincs-streaming-normalizer",
            idempotency_key=f"{idempotency_key}:index-manifest",
        )
        role_manifest_artifacts = {
            item.logical_role: _artifact_reference(
                self.artifacts.put_json(
                    workflow_id=workflow_id,
                    value=item.model_dump(mode="json"),
                    artifact_type="lincs_role_normalization_manifest",
                    logical_name=(f"lincs-{cache_identity}-{item.logical_role}-manifest.json"),
                    producer="lincs-streaming-normalizer",
                    idempotency_key=(f"{idempotency_key}:role-manifest:{item.logical_role}"),
                ),
                "lincs_role_normalization_manifest",
            )
            for item in role_manifests
        }
        normalization_manifest = LincsStreamingNormalizationManifest(
            release_id=manifest.release_id,
            source_release=manifest.source_release,
            source_locator=manifest.source_locator,
            release_manifest_fingerprint=manifest.manifest_fingerprint,
            cache_identity=cache_identity,
            role_artifacts=role_manifests,
            role_manifest_artifacts=role_manifest_artifacts,
            index_manifest_artifact=_artifact_reference(index_descriptor),
            index_fingerprint=index_fingerprint,
            bundle_fingerprint=bundle_fingerprint,
            total_normalization_wall_time_ms=sum(
                item.normalization_wall_time_ms for item in role_manifests
            ),
            total_indexing_wall_time_ms=sum(item.indexing_wall_time_ms for item in role_manifests),
            peak_resident_memory_bytes=peak_rss,
            expression_values_retrieved=False,
        )
        descriptor = self.artifacts.put_json(
            workflow_id=workflow_id,
            value=normalization_manifest.model_dump(mode="json"),
            artifact_type="lincs_normalization_manifest",
            logical_name=f"lincs-normalization-{cache_identity}.json",
            producer="lincs-streaming-normalizer",
            idempotency_key=f"{idempotency_key}:normalization-manifest",
        )
        return normalization_manifest, _artifact_reference(
            descriptor, "lincs_normalization_manifest"
        )

    def retrieve(
        self,
        request: LincsMetadataRetrievalInput,
        invocation: ToolInvocation,
    ) -> LincsStreamingRetrievalOutput:
        if invocation.workflow_id is None:
            raise ValueError("LINCS retrieval requires a durable workflow identifier")
        task = self.retrieval_task(
            workflow_id=invocation.workflow_id,
            task_id=request.ledger_record.task_id,
            release_id=request.release_id,
            idempotency_key=invocation.idempotency_key or request.ledger_record.task_id,
        )
        execution = self.executor.execute(
            task,
            request.ledger_record,
            parser=parse_lincs_metadata_resource_compact,
        )
        if not execution.completion_proof.completed:
            return LincsStreamingRetrievalOutput(execution=execution)
        release = self.registry.release(request.release_id)
        cache_identity = _normalization_cache_identity(release, execution)
        cached = self._load_cached_manifest(invocation.workflow_id, cache_identity)
        if cached is not None:
            manifest, manifest_reference = cached
            return LincsStreamingRetrievalOutput(
                execution=execution,
                normalization_manifest=manifest,
                normalization_manifest_artifact=manifest_reference,
                normalization_ran=False,
                indexing_ran=False,
            )
        manifest, manifest_reference = self._normalize_and_index(
            workflow_id=invocation.workflow_id,
            manifest=release,
            outcome=execution,
            cache_identity=cache_identity,
            idempotency_key=invocation.idempotency_key or request.ledger_record.task_id,
        )
        return LincsStreamingRetrievalOutput(
            execution=execution,
            normalization_manifest=manifest,
            normalization_manifest_artifact=manifest_reference,
            normalization_ran=True,
            indexing_ran=True,
        )

    def _load_normalization_manifest(
        self, reference: ArtifactReference, workflow_id: str | None
    ) -> LincsStreamingNormalizationManifest:
        descriptor, content = self.artifacts.get(reference.artifact_id)
        if workflow_id is not None and descriptor.workflow_id != workflow_id:
            raise ValueError("LINCS artifact does not belong to the invoking workflow")
        if descriptor.sha256 != reference.sha256:
            raise ValueError("LINCS artifact reference hash does not match")
        manifest = LincsStreamingNormalizationManifest.model_validate_json(content)
        for role in manifest.role_artifacts:
            self.artifacts.verified_path(role.normalized_artifact.artifact_id)
        for reference in manifest.role_manifest_artifacts.values():
            self.artifacts.verified_path(reference.artifact_id)
        return manifest

    def indexes(
        self, request: LincsDiskIndexInput, invocation: ToolInvocation
    ) -> LincsDiskIndexOutput:
        manifest = self._load_normalization_manifest(
            request.normalization_manifest_artifact, invocation.workflow_id
        )
        descriptor, content = self.artifacts.get(manifest.index_manifest_artifact.artifact_id)
        index_manifest = LincsDiskIndexManifest.model_validate_json(content)
        if descriptor.sha256 != manifest.index_manifest_artifact.sha256:
            raise ValueError("LINCS index manifest hash does not match")
        return LincsDiskIndexOutput(
            index_manifest_artifact=manifest.index_manifest_artifact,
            index_fingerprint=index_manifest.index_fingerprint,
            total_indexed_rows=index_manifest.total_indexed_rows,
            total_artifact_bytes=index_manifest.total_artifact_bytes,
            index_rebuilt=False,
        )

    def coverage(
        self, request: LincsDiskCoverageInput, invocation: ToolInvocation
    ) -> LincsDiskCoverageOutput:
        manifest = self._load_normalization_manifest(
            request.normalization_manifest_artifact, invocation.workflow_id
        )
        if invocation.workflow_id is None:
            raise ValueError("LINCS coverage requires a durable workflow identifier")
        return _compute_disk_coverage(
            artifacts=self.artifacts,
            workflow_id=invocation.workflow_id,
            request=request,
            manifest=manifest,
            idempotency_key=invocation.idempotency_key or manifest.bundle_fingerprint,
        )


def _fetch_candidates(
    connection: sqlite3.Connection, compound: CompoundIdentityRecord
) -> tuple[list[tuple[str, str | None]], str | None]:
    candidates: list[tuple[str, str | None]] = []
    method: str | None = None
    if compound.inchikey:
        candidates = connection.execute(
            """
            SELECT record_id, pert_id FROM records
            WHERE row_status != 'excluded' AND inchikey = ? ORDER BY record_id
            """,
            (compound.inchikey,),
        ).fetchall()
        method = "exact_full_inchikey" if candidates else None
    if not candidates and compound.pubchem_cid:
        candidates = connection.execute(
            """
            SELECT record_id, pert_id FROM records
            WHERE row_status != 'excluded' AND pubchem_cid = ? ORDER BY record_id
            """,
            (compound.pubchem_cid,),
        ).fetchall()
        method = "exact_pubchem_cid" if candidates else None
    provider_id = compound.reviewed_provider_identifiers.get("lincs-l1000") or (
        compound.reviewed_provider_identifiers.get("lincs")
    )
    if not candidates and provider_id:
        candidates = connection.execute(
            """
            SELECT record_id, pert_id FROM records
            WHERE row_status != 'excluded' AND pert_id = ? ORDER BY record_id
            """,
            (provider_id,),
        ).fetchall()
        method = "exact_reviewed_provider_identifier" if candidates else None
    if not candidates:
        names = sorted(
            {name.casefold() for name in [compound.canonical_name, *compound.synonyms] if name}
        )
        if names:
            placeholders = ",".join("?" for _ in names)
            candidates = connection.execute(
                f"""
                SELECT r.record_id, r.pert_id
                FROM exact_names n JOIN records r ON r.record_id = n.record_id
                WHERE r.row_status != 'excluded' AND n.exact_name IN ({placeholders})
                GROUP BY r.record_id, r.pert_id ORDER BY r.record_id
                """,
                names,
            ).fetchall()
            method = "unique_exact_name_or_synonym" if candidates else None
    return candidates, method


def _compound_match(
    connection: sqlite3.Connection, compound: CompoundIdentityRecord
) -> LincsCompoundMatch:
    candidates, method = _fetch_candidates(connection, compound)
    provider_id = compound.reviewed_provider_identifiers.get("lincs-l1000") or (
        compound.reviewed_provider_identifiers.get("lincs")
    )
    record_ids = [item[0] for item in candidates]
    pert_ids = sorted({item[1] for item in candidates if item[1]})
    if len(candidates) == 1:
        return LincsCompoundMatch(
            compound_id=compound.compound_id,
            status=LincsCompoundMatchStatus.EXACT_MATCH,
            match_method=method,
            perturbagen_record_ids=record_ids,
            pert_ids=pert_ids,
            explanation="One exact reviewed LINCS metadata mapping was found.",
        )
    if len(candidates) > 1:
        return LincsCompoundMatch(
            compound_id=compound.compound_id,
            status=LincsCompoundMatchStatus.AMBIGUOUS_MATCH,
            match_method=method,
            perturbagen_record_ids=record_ids,
            pert_ids=pert_ids,
            explanation="Multiple exact LINCS mappings exist; none was selected silently.",
        )
    if provider_id:
        status = LincsCompoundMatchStatus.SOURCE_MAPPING_MISSING
        explanation = "The reviewed LINCS provider identifier is absent from the release."
    elif not any(
        [compound.inchikey, compound.pubchem_cid, compound.canonical_name, *compound.synonyms]
    ):
        status = LincsCompoundMatchStatus.IDENTIFIER_MISSING
        explanation = "No exact identifier or reviewed name is available for matching."
    else:
        status = LincsCompoundMatchStatus.NOT_FOUND
        explanation = "No exact LINCS metadata mapping was found."
    return LincsCompoundMatch(
        compound_id=compound.compound_id,
        status=status,
        explanation=explanation,
    )


def _context_matches(
    signature: sqlite3.Row,
    organism: str | None,
    context: LincsContextFilter,
) -> bool:
    return all(
        (
            not context.cell_ids or signature["cell_id"] in context.cell_ids,
            not context.organisms or organism in context.organisms,
            not context.doses or signature["dose"] in context.doses,
            not context.exposure_times or signature["exposure_time"] in context.exposure_times,
            not context.quality_statuses or signature["quality_status"] in context.quality_statuses,
        )
    )


def _compute_disk_coverage(
    *,
    artifacts: LocalArtifactStore,
    workflow_id: str,
    request: LincsDiskCoverageInput,
    manifest: LincsStreamingNormalizationManifest,
    idempotency_key: str,
) -> LincsDiskCoverageOutput:
    role_paths = {
        role.logical_role: artifacts.verified_path(role.normalized_artifact.artifact_id)[1]
        for role in manifest.role_artifacts
    }
    input_fingerprint = deterministic_fingerprint(
        {
            "bundle_fingerprint": manifest.bundle_fingerprint,
            "compounds": [item.model_dump(mode="json") for item in request.compounds],
            "contexts": [item.model_dump(mode="json") for item in request.contexts],
            "coverage_schema_version": LINCS_COVERAGE_SCHEMA_VERSION,
        }
    )
    logical_name = f"lincs-coverage-{input_fingerprint}.sqlite"
    existing = artifacts.find_by_logical_name(workflow_id, logical_name)
    if existing is not None:
        descriptor, path = artifacts.verified_path(existing.id)
        return _coverage_output_from_artifact(path, descriptor, manifest, request.preview_limit)

    with tempfile.TemporaryDirectory(prefix="lincs-coverage-", dir=artifacts.root) as temp:
        target = Path(temp) / "coverage.sqlite"
        output = sqlite3.connect(target)
        output.row_factory = sqlite3.Row
        pert = sqlite3.connect(role_paths["pert_info"])
        sig = sqlite3.connect(role_paths["sig_info"])
        sig.row_factory = sqlite3.Row
        cell = sqlite3.connect(role_paths["cell_info"])
        try:
            output.executescript(
                """
                CREATE TABLE matches (
                  compound_id TEXT PRIMARY KEY, status TEXT NOT NULL, match_method TEXT,
                  perturbagen_record_ids_json TEXT NOT NULL, pert_ids_json TEXT NOT NULL,
                  explanation TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE signatures (
                  compound_id TEXT NOT NULL, sig_id TEXT NOT NULL, pert_id TEXT,
                  cell_id TEXT, organism TEXT, dose TEXT, exposure_time TEXT,
                  quality_status TEXT,
                  PRIMARY KEY (compound_id, sig_id)
                ) WITHOUT ROWID;
                CREATE TABLE context_members (
                  context_id TEXT NOT NULL, compound_id TEXT NOT NULL, sig_id TEXT NOT NULL,
                  PRIMARY KEY (context_id, compound_id, sig_id)
                ) WITHOUT ROWID;
                CREATE TABLE aggregates (
                  dimension TEXT NOT NULL, value TEXT NOT NULL, signature_count INTEGER NOT NULL,
                  PRIMARY KEY (dimension, value)
                ) WITHOUT ROWID;
                CREATE TABLE metadata (
                  key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )
            cell_organisms = {
                row[0]: row[1]
                for row in cell.execute(
                    "SELECT cell_id, organism FROM records WHERE cell_id IS NOT NULL"
                )
            }
            for compound in request.compounds:
                match = _compound_match(pert, compound)
                output.execute(
                    "INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        match.compound_id,
                        match.status.value,
                        match.match_method,
                        json.dumps(match.perturbagen_record_ids, separators=(",", ":")),
                        json.dumps(match.pert_ids, separators=(",", ":")),
                        match.explanation,
                    ),
                )
                if match.status is not LincsCompoundMatchStatus.EXACT_MATCH:
                    continue
                for pert_id in match.pert_ids:
                    rows = sig.execute(
                        """
                        SELECT sig_id, pert_id, cell_id, dose, exposure_time, quality_status
                        FROM records
                        WHERE pert_id = ? AND measured_chemical_perturbation = 1
                          AND row_status = 'valid' AND sig_id IS NOT NULL
                        ORDER BY sig_id
                        """,
                        (pert_id,),
                    )
                    for signature in rows:
                        organism = cell_organisms.get(signature["cell_id"])
                        output.execute(
                            "INSERT OR IGNORE INTO signatures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                compound.compound_id,
                                signature["sig_id"],
                                signature["pert_id"],
                                signature["cell_id"],
                                organism,
                                signature["dose"],
                                signature["exposure_time"],
                                signature["quality_status"],
                            ),
                        )
                        for context in request.contexts:
                            if _context_matches(signature, organism, context):
                                output.execute(
                                    "INSERT OR IGNORE INTO context_members VALUES (?, ?, ?)",
                                    (context.context_id, compound.compound_id, signature["sig_id"]),
                                )
            for dimension, column in (
                ("cell", "cell_id"),
                ("organism", "organism"),
                ("dose", "dose"),
                ("exposure_time", "exposure_time"),
                ("quality_status", "quality_status"),
            ):
                output.execute(
                    f"""
                    INSERT INTO aggregates
                    SELECT ?, COALESCE({column}, 'missing'), COUNT(DISTINCT sig_id)
                    FROM signatures GROUP BY COALESCE({column}, 'missing')
                    """,
                    (dimension,),
                )
            exclusion_reasons: Counter[str] = Counter()
            missing_fields: Counter[str] = Counter()
            for _role, path in role_paths.items():
                role_connection = sqlite3.connect(path)
                try:
                    for reason, count in role_connection.execute(
                        """
                        SELECT exclusion_reason, COUNT(*) FROM records
                        WHERE exclusion_reason IS NOT NULL GROUP BY exclusion_reason
                        """
                    ):
                        exclusion_reasons[str(reason)] += int(count)
                finally:
                    role_connection.close()
            for column in ("cell_id", "dose", "exposure_time", "quality_status"):
                count = output.execute(
                    f"SELECT COUNT(*) FROM signatures WHERE {column} IS NULL"
                ).fetchone()[0]
                if count:
                    missing_fields[column] = int(count)
            metadata = {
                "release_id": manifest.release_id,
                "bundle_fingerprint": manifest.bundle_fingerprint,
                "input_fingerprint": input_fingerprint,
                "total_input_compounds": len(request.compounds),
                "missing_metadata_fields": dict(sorted(missing_fields.items())),
                "exclusion_reasons": dict(sorted(exclusion_reasons.items())),
                "contexts": [item.model_dump(mode="json") for item in request.contexts],
                "expression_values_retrieved": False,
            }
            output.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    (key, json.dumps(value, sort_keys=True, separators=(",", ":")))
                    for key, value in sorted(metadata.items())
                ],
            )
            output.executescript(
                """
                CREATE INDEX idx_signatures_compound ON signatures(compound_id);
                CREATE INDEX idx_signatures_sig_id ON signatures(sig_id);
                CREATE INDEX idx_context_members_context ON context_members(context_id);
                """
            )
            output.commit()
            output.execute("VACUUM")
        finally:
            pert.close()
            sig.close()
            cell.close()
            output.close()
        descriptor = artifacts.put_file(
            workflow_id=workflow_id,
            source_path=target,
            mime_type="application/octet-stream",
            artifact_type="lincs_metadata_coverage_sqlite",
            logical_name=logical_name,
            producer="lincs-disk-coverage",
            idempotency_key=f"{idempotency_key}:coverage",
        )
    verified, path = artifacts.verified_path(descriptor.id)
    return _coverage_output_from_artifact(path, verified, manifest, request.preview_limit)


def _coverage_output_from_artifact(
    path: Path,
    descriptor: ArtifactDescriptor,
    manifest: LincsStreamingNormalizationManifest,
    preview_limit: int,
) -> LincsDiskCoverageOutput:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            row["key"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT key, value_json FROM metadata")
        }
        counts = dict(
            connection.execute("SELECT status, COUNT(*) FROM matches GROUP BY status").fetchall()
        )
        matches = [
            LincsCompoundMatch(
                compound_id=row["compound_id"],
                status=LincsCompoundMatchStatus(row["status"]),
                match_method=row["match_method"],
                perturbagen_record_ids=json.loads(row["perturbagen_record_ids_json"]),
                pert_ids=json.loads(row["pert_ids_json"]),
                explanation=row["explanation"],
            )
            for row in connection.execute(
                "SELECT * FROM matches ORDER BY compound_id LIMIT ?", (preview_limit,)
            )
        ]
        exact_sig_count = int(
            connection.execute("SELECT COUNT(DISTINCT sig_id) FROM signatures").fetchone()[0]
        )
        sig_preview = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT sig_id FROM signatures ORDER BY sig_id LIMIT ?",
                (preview_limit,),
            )
        ]
        context_ids = [str(item["context_id"]) for item in metadata["contexts"]]
        views: list[LincsCoverageContextSummary] = []
        for context_id in context_ids:
            compound_count, signature_count = connection.execute(
                """
                SELECT COUNT(DISTINCT compound_id), COUNT(DISTINCT sig_id)
                FROM context_members WHERE context_id = ?
                """,
                (context_id,),
            ).fetchone()
            compound_preview = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT compound_id FROM context_members
                    WHERE context_id = ? ORDER BY compound_id LIMIT ?
                    """,
                    (context_id, preview_limit),
                )
            ]
            context_sig_preview = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT sig_id FROM context_members
                    WHERE context_id = ? ORDER BY sig_id LIMIT ?
                    """,
                    (context_id, preview_limit),
                )
            ]
            views.append(
                LincsCoverageContextSummary(
                    view_id=f"individual:{context_id}",
                    operator="individual",
                    context_ids=[context_id],
                    compound_count=int(compound_count),
                    signature_count=int(signature_count),
                    compound_ids_preview=compound_preview,
                    exact_sig_ids_preview=context_sig_preview,
                )
            )
        if context_ids:
            placeholders = ",".join("?" for _ in context_ids)
            union_compounds = int(
                connection.execute(
                    f"""
                    SELECT COUNT(DISTINCT compound_id) FROM context_members
                    WHERE context_id IN ({placeholders})
                    """,
                    context_ids,
                ).fetchone()[0]
            )
            union_sigs = int(
                connection.execute(
                    f"""
                    SELECT COUNT(DISTINCT sig_id) FROM context_members
                    WHERE context_id IN ({placeholders})
                    """,
                    context_ids,
                ).fetchone()[0]
            )
            every_compound_count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM (
                      SELECT compound_id FROM context_members
                      WHERE context_id IN ({placeholders})
                      GROUP BY compound_id HAVING COUNT(DISTINCT context_id) = ?
                    )
                    """,
                    [*context_ids, len(context_ids)],
                ).fetchone()[0]
            )
            every_compound_preview = [
                row[0]
                for row in connection.execute(
                    f"""
                    SELECT compound_id FROM context_members
                    WHERE context_id IN ({placeholders})
                    GROUP BY compound_id HAVING COUNT(DISTINCT context_id) = ?
                    ORDER BY compound_id LIMIT ?
                    """,
                    [*context_ids, len(context_ids), preview_limit],
                )
            ]
            union_compound_preview = [
                row[0]
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT compound_id FROM context_members
                    WHERE context_id IN ({placeholders})
                    ORDER BY compound_id LIMIT ?
                    """,
                    [*context_ids, preview_limit],
                )
            ]
            union_sig_preview = [
                row[0]
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT sig_id FROM context_members
                    WHERE context_id IN ({placeholders}) ORDER BY sig_id LIMIT ?
                    """,
                    [*context_ids, preview_limit],
                )
            ]
            every_query = f"""
                WITH every_compound AS (
                  SELECT compound_id FROM context_members
                  WHERE context_id IN ({placeholders})
                  GROUP BY compound_id HAVING COUNT(DISTINCT context_id) = ?
                )
                SELECT DISTINCT cm.sig_id
                FROM context_members cm
                JOIN every_compound ec ON ec.compound_id = cm.compound_id
                WHERE cm.context_id IN ({placeholders})
            """
            every_parameters = [*context_ids, len(context_ids), *context_ids]
            every_sig_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ({every_query})", every_parameters
                ).fetchone()[0]
            )
            every_sig_preview = [
                row[0]
                for row in connection.execute(
                    f"{every_query} ORDER BY cm.sig_id LIMIT ?",
                    [*every_parameters, preview_limit],
                )
            ]
            union_operators: tuple[Literal["union", "at_least_one"], ...] = (
                "union",
                "at_least_one",
            )
            for operator in union_operators:
                views.append(
                    LincsCoverageContextSummary(
                        view_id=f"{operator}:" + "+".join(context_ids),
                        operator=operator,
                        context_ids=context_ids,
                        compound_count=union_compounds,
                        signature_count=union_sigs,
                        compound_ids_preview=union_compound_preview,
                        exact_sig_ids_preview=union_sig_preview,
                    )
                )
            intersection_operators: tuple[Literal["intersection", "every"], ...] = (
                "intersection",
                "every",
            )
            for intersection_operator in intersection_operators:
                views.append(
                    LincsCoverageContextSummary(
                        view_id=f"{intersection_operator}:" + "+".join(context_ids),
                        operator=intersection_operator,
                        context_ids=context_ids,
                        compound_count=every_compound_count,
                        signature_count=every_sig_count,
                        compound_ids_preview=every_compound_preview,
                        exact_sig_ids_preview=every_sig_preview,
                    )
                )
        exact = int(counts.get(LincsCompoundMatchStatus.EXACT_MATCH.value, 0))
        ambiguous = int(counts.get(LincsCompoundMatchStatus.AMBIGUOUS_MATCH.value, 0))
        total = int(metadata["total_input_compounds"])
        with_signatures = int(
            connection.execute("SELECT COUNT(DISTINCT compound_id) FROM signatures").fetchone()[0]
        )
        aggregate_distinct_value_counts = dict(
            connection.execute(
                "SELECT dimension, COUNT(*) FROM aggregates GROUP BY dimension"
            ).fetchall()
        )
        aggregate_previews: dict[str, dict[str, int]] = {}
        for dimension in sorted(aggregate_distinct_value_counts):
            aggregate_previews[dimension] = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    """
                    SELECT value, signature_count FROM aggregates
                    WHERE dimension = ? ORDER BY value LIMIT ?
                    """,
                    (dimension, preview_limit),
                )
            }
    finally:
        connection.close()
    coverage_fingerprint = deterministic_fingerprint(
        {
            "bundle_fingerprint": manifest.bundle_fingerprint,
            "coverage_artifact_sha256": descriptor.sha256,
            "coverage_schema_version": LINCS_COVERAGE_SCHEMA_VERSION,
        }
    )
    return LincsDiskCoverageOutput(
        release_id=manifest.release_id,
        bundle_fingerprint=manifest.bundle_fingerprint,
        coverage_artifact=_artifact_reference(descriptor),
        total_input_compounds=total,
        exact_perturbagen_matches=exact,
        ambiguous_matches=ambiguous,
        unmatched_compounds=total - exact - ambiguous,
        compounds_with_measured_chemical_signatures=with_signatures,
        exact_available_signature_count=exact_sig_count,
        compound_matches_preview=matches,
        exact_sig_ids_preview=sig_preview,
        context_views=views,
        aggregate_distinct_value_counts={
            key: int(value) for key, value in aggregate_distinct_value_counts.items()
        },
        aggregate_previews=aggregate_previews,
        missing_metadata_fields=metadata["missing_metadata_fields"],
        exclusion_reasons=metadata["exclusion_reasons"],
        coverage_fingerprint=coverage_fingerprint,
        complete_result_in_artifact=True,
        expression_values_retrieved=False,
    )
