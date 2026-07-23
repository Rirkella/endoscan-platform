"""Typed contracts and deterministic boundaries for endpoint completion.

All documents in this module are immutable workflow artifacts.  Large tables
and models are represented by content-addressed references, never inline.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import LocalArtifactStore
from .discovery_strategy import (
    ArtifactReference,
    CombinationCoverage,
    CombinationCoverageSet,
    EvidenceRole,
    HydratedSourceSet,
    ProposalStatus,
    StrategyProposal,
    StrategyProposalSet,
    deterministic_fingerprint,
)

ENDPOINT_LIFECYCLE_VERSION: Literal["1.0.0"] = "1.0.0"


def utc_now() -> datetime:
    return datetime.now(UTC)


class LifecycleContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0.0"] = ENDPOINT_LIFECYCLE_VERSION


class ValidationStatusMatrix(LifecycleContract):
    implementation_status: Literal["implemented"] = "implemented"
    offline_validation_status: Literal["offline_validated"] = "offline_validated"
    live_validation_status: Literal["live_validated", "live_validation_pending"] = (
        "live_validation_pending"
    )
    scientific_validation_status: Literal[
        "scientifically_validated", "scientifically_validation_pending"
    ] = "scientifically_validation_pending"
    limitations: list[str] = Field(default_factory=list, max_length=100)


class StrategyAgentInput(LifecycleContract):
    workflow_id: str
    approved_endpoint_specification: dict[str, Any]
    hydrated_sources: HydratedSourceSet
    coverage: CombinationCoverageSet
    allowed_scientific_policies: list[str] = Field(min_length=1, max_length=100)
    quality_constraints: list[str] = Field(min_length=1, max_length=100)


class AssemblyStrategyAgent(Protocol):
    def generate(self, request: StrategyAgentInput) -> StrategyProposalSet: ...


class DeterministicAssemblyStrategyAgent:
    """Offline implementation of the production agent boundary."""

    def generate(self, request: StrategyAgentInput) -> StrategyProposalSet:
        known_sources = {item.hydrated_source_id for item in request.hydrated_sources.sources}
        proposals: list[StrategyProposal] = []
        viable = sorted(
            request.coverage.combinations,
            key=lambda item: (-item.overlap_count, item.coverage_id),
        )
        for item in viable[:5]:
            sources = [*item.activity_source_ids, item.transcriptomic_source_id]
            if not set(sources).issubset(known_sources):
                raise ValueError("Coverage references an unknown hydrated source.")
            policy = "source_backed_activity_call"
            if policy not in request.allowed_scientific_policies:
                continue
            blocking_flags = {
                "no_stable_identifier_overlap",
                "transcriptomic_source_not_joinability_eligible",
                "expression_extraction_path_unavailable",
                "incomplete_transcriptomic_context",
                "activity_labels_unavailable",
            }
            reviewable = (
                item.overlap_count > 0
                and item.expected_assembled_sample_count > 0
                and not (set(item.quality_flags) & blocking_flags)
            )
            proposals.append(
                StrategyProposal(
                    proposal_id=(
                        "strategy-"
                        f"{deterministic_fingerprint({'coverage': item.coverage_id})[:20]}"
                    ),
                    included_source_combination=sources,
                    modalities=[item.requested_modality],
                    proposed_context=item.context,
                    proposed_label_policy={"operator": policy, "preserve_raw_outcomes": True},
                    expected_dataset_size=item.expected_assembled_sample_count
                    or item.overlap_count,
                    expected_unique_compounds=item.expected_unique_compound_count
                    or item.overlap_count,
                    expected_class_balance={
                        "active": item.active_count or 0,
                        "inactive": item.inactive_count or 0,
                    },
                    identifier_losses=item.identifier_resolution_losses,
                    metadata_losses=item.missing_metadata,
                    scientific_strengths=[
                        "Uses source-backed labels and a verified identifier overlap."
                    ],
                    scientific_risks=list(item.limitations),
                    exclusions=["Ambiguous labels", "Unresolved compound identifiers"],
                    provenance_references=item.computation_provenance,
                    proposal_status=(
                        ProposalStatus.VIABLE if reviewable else ProposalStatus.BLOCKED
                    ),
                    context_rules=item.context,
                    quality_constraints=request.quality_constraints,
                    advantages=["Deterministic and fully provenance-bound."],
                    limitations=item.limitations,
                    bias_risks=["Public-source and experimental-context coverage bias."],
                    required_postapproval_data_access=[
                        "approved activity rows",
                        "approved selective transcriptomic expression slice",
                    ],
                )
            )
        return StrategyProposalSet(
            workflow_id=request.workflow_id,
            discovery_round=request.coverage.discovery_round,
            proposals=proposals,
        )


def _stable_compound_ids(metadata: dict[str, Any]) -> set[str]:
    values = metadata.get("stable_compound_ids", metadata.get("compound_ids", []))
    if not isinstance(values, list):
        return set()
    return {
        normalized for value in values if (normalized := _canonical_identifier(value)) is not None
    }


def _canonical_identifier(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if re.fullmatch(r"(?:CID:)?[1-9][0-9]{0,11}", text, re.IGNORECASE):
        return f"CID:{text.split(':')[-1]}"
    if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", text.upper()):
        return text.upper()
    return text.upper()


def _summary_value(summary: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = summary.get(key)
        if isinstance(value, int):
            return value
    by_task = summary.get("by_task")
    if isinstance(by_task, dict):
        total = 0
        found = False
        for task_summary in by_task.values():
            if not isinstance(task_summary, dict):
                continue
            for key in keys:
                value = task_summary.get(key)
                if isinstance(value, int):
                    total += value
                    found = True
                    break
        if found:
            return total
    return 0


def _sqlite_artifacts(
    source: Any,
    artifact_store: LocalArtifactStore | None,
) -> list[Path]:
    if artifact_store is None:
        return []
    paths: list[Path] = []
    for reference in source.evidence_artifacts:
        try:
            _, path = artifact_store.verified_path(reference.artifact_id)
            with path.open("rb") as handle:
                if handle.read(16) == b"SQLite format 3\x00":
                    paths.append(path)
        except (KeyError, OSError, ValueError):
            continue
    return paths


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _identity_aliases(
    hydrated_sources: HydratedSourceSet,
    artifact_store: LocalArtifactStore | None,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    visited: set[Path] = set()
    for source in hydrated_sources.sources:
        for path in _sqlite_artifacts(source, artifact_store):
            if path in visited:
                continue
            visited.add(path)
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "compound_index" in tables:
                    columns = _columns(connection, "compound_index")
                    fields = [
                        name
                        for name in ("compound_identifier", "cid", "sid", "inchikey")
                        if name in columns
                    ]
                    if fields:
                        sql = "SELECT " + ", ".join(fields) + " FROM compound_index"
                        for row in connection.execute(sql):
                            normalized = [
                                item
                                for value in row
                                if (item := _canonical_identifier(value)) is not None
                            ]
                            canonical = next(
                                (item for item in normalized if item.startswith("CID:")),
                                next(
                                    (
                                        item
                                        for item in normalized
                                        if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", item)
                                    ),
                                    normalized[0] if normalized else None,
                                ),
                            )
                            if canonical:
                                aliases.update({item: canonical for item in normalized})
                if "records" in tables:
                    columns = _columns(connection, "records")
                    if {"pert_id", "pubchem_cid", "inchikey"}.issubset(columns):
                        for row in connection.execute(
                            "SELECT pert_id, pubchem_cid, inchikey FROM records"
                        ):
                            normalized = [
                                item
                                for value in row
                                if (item := _canonical_identifier(value)) is not None
                            ]
                            canonical = next(
                                (item for item in normalized if item.startswith("CID:")),
                                next(
                                    (
                                        item
                                        for item in normalized
                                        if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", item)
                                    ),
                                    normalized[0] if normalized else None,
                                ),
                            )
                            if canonical:
                                aliases.update({item: canonical for item in normalized})
    return aliases


def _resolved_identifier(value: Any, aliases: dict[str, str]) -> str | None:
    normalized = _canonical_identifier(value)
    if normalized is None:
        return None
    return aliases.get(normalized, normalized)


def _activity_evidence(
    source: Any,
    artifact_store: LocalArtifactStore | None,
    aliases: dict[str, str],
) -> tuple[set[str], list[tuple[str, str | None]]]:
    identifiers = {
        aliases.get(item, item) for item in _stable_compound_ids(source.verified_metadata)
    }
    rows: list[tuple[str, str | None]] = []
    source_identifier = source.source_identifier.split(":", 1)[-1]
    for path in _sqlite_artifacts(source, artifact_store):
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "activity_records" not in tables:
                continue
            columns = _columns(connection, "activity_records")
            if not {"assay_identifier", "compound_identifier"}.issubset(columns):
                continue
            cid = "cid" if "cid" in columns else "NULL"
            call = "activity_call" if "activity_call" in columns else "NULL"
            sql = (
                f"SELECT compound_identifier, {cid}, {call} FROM activity_records "
                "WHERE assay_identifier IN (?, ?)"
            )
            for compound_identifier, pubchem_cid, activity_call in connection.execute(
                sql, (source_identifier, source.source_identifier)
            ):
                stable = _resolved_identifier(pubchem_cid or compound_identifier, aliases)
                if stable is None:
                    continue
                identifiers.add(stable)
                rows.append((stable, str(activity_call).strip().lower() if activity_call else None))
    return identifiers, rows


def _transcriptomic_evidence(
    source: Any,
    artifact_store: LocalArtifactStore | None,
    aliases: dict[str, str],
) -> tuple[set[str], list[tuple[str, str | None, str | None, str | None]]]:
    identifiers = {
        aliases.get(item, item) for item in _stable_compound_ids(source.verified_metadata)
    }
    profiles: list[tuple[str, str | None, str | None, str | None]] = []
    for path in _sqlite_artifacts(source, artifact_store):
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "transcriptomic_records" in tables:
                sql = """
                    SELECT compound_identifier, biological_model, dose, exposure_time
                    FROM transcriptomic_records
                    WHERE (accession=? OR source_identifier=?)
                      AND compound_identifier IS NOT NULL
                """
                for compound, cell, dose, exposure_time in connection.execute(
                    sql, (source.source_identifier, source.source_identifier)
                ):
                    stable = _resolved_identifier(compound, aliases)
                    if stable is not None:
                        identifiers.add(stable)
                        profiles.append((stable, cell, dose, exposure_time))
            if "records" not in tables:
                continue
            columns = _columns(connection, "records")
            if {
                "pert_id",
                "cell_id",
                "dose",
                "exposure_time",
                "measured_chemical_perturbation",
            }.issubset(columns):
                for pert_id, cell, dose, exposure_time in connection.execute(
                    """
                    SELECT pert_id, cell_id, dose, exposure_time FROM records
                    WHERE measured_chemical_perturbation=1 AND row_status='valid'
                    """
                ):
                    stable = _resolved_identifier(pert_id, aliases)
                    if stable is not None:
                        identifiers.add(stable)
                        profiles.append((stable, cell, dose, exposure_time))
    return identifiers, profiles


def calculate_combination_coverage(
    workflow_id: str,
    discovery_round: int,
    hydrated_sources: HydratedSourceSet,
    *,
    artifact_store: LocalArtifactStore | None = None,
) -> CombinationCoverageSet:
    """Evaluate every activity/transcriptomic source pair without inventing labels.

    Provider row tables remain artifact-backed.  Compact source metadata supplies only
    stable identifier indexes and truthful summaries needed for deterministic overlap.
    """

    activity_sources = sorted(
        (
            source
            for source in hydrated_sources.sources
            if source.evidence_role is EvidenceRole.ACTIVITY
        ),
        key=lambda source: source.hydrated_source_id,
    )
    transcriptomic_sources = sorted(
        (
            source
            for source in hydrated_sources.sources
            if source.evidence_role is EvidenceRole.TRANSCRIPTOMIC
        ),
        key=lambda source: source.hydrated_source_id,
    )
    aliases = _identity_aliases(hydrated_sources, artifact_store)
    combinations: list[CombinationCoverage] = []
    for activity in activity_sources:
        activity_ids, activity_rows = _activity_evidence(activity, artifact_store, aliases)
        for transcriptomic in transcriptomic_sources:
            transcriptomic_ids, transcriptomic_profiles = _transcriptomic_evidence(
                transcriptomic, artifact_store, aliases
            )
            overlap = activity_ids & transcriptomic_ids
            activity_summary = activity.coverage_summary
            transcriptomic_summary = transcriptomic.coverage_summary
            overlap_profiles = [
                profile for profile in transcriptomic_profiles if profile[0] in overlap
            ]
            context = dict(transcriptomic.experimental_context)
            if overlap_profiles:
                context.update(
                    {
                        "cell": sorted({item[1] for item in overlap_profiles if item[1]}),
                        "dose": sorted({item[2] for item in overlap_profiles if item[2]}),
                        "time": sorted({item[3] for item in overlap_profiles if item[3]}),
                    }
                )
            refs = {
                (reference.artifact_id, reference.sha256): reference
                for reference in [
                    *activity.evidence_artifacts,
                    *transcriptomic.evidence_artifacts,
                ]
            }
            missing: dict[str, int] = {}
            for field in ("cell", "dose", "time"):
                if not context.get(field):
                    missing[field] = _summary_value(
                        transcriptomic_summary, "profile_count", "metadata_rows"
                    )
            joined_activity_rows = [row for row in activity_rows if row[0] in overlap]
            active = sum(row[1] == "active" for row in joined_activity_rows)
            inactive = sum(row[1] == "inactive" for row in joined_activity_rows)
            ambiguous = sum(row[1] not in {"active", "inactive"} for row in joined_activity_rows)
            if not activity_rows:
                active = _summary_value(activity_summary, "active_count")
                inactive = _summary_value(activity_summary, "inactive_count")
                ambiguous = _summary_value(activity_summary, "ambiguous_count")
            identifier_universe = activity_ids | transcriptomic_ids
            stable_coverage = (
                len(overlap) / len(identifier_universe) if identifier_universe else 0.0
            )
            modality = activity.modality or "unspecified"
            eligibility = transcriptomic.verified_metadata.get("joinability_eligible")
            expression_path = transcriptomic.verified_metadata.get("expression_extraction_path")
            quality_flags: list[str] = []
            if not overlap:
                quality_flags.append("no_stable_identifier_overlap")
            if eligibility is not True:
                quality_flags.append("transcriptomic_source_not_joinability_eligible")
            if expression_path in {None, "", "unavailable"}:
                quality_flags.append("expression_extraction_path_unavailable")
            if any(not context.get(field) for field in ("cell", "dose", "time")):
                quality_flags.append("incomplete_transcriptomic_context")
            if not activity.label_or_activity_fields_available:
                quality_flags.append("activity_labels_unavailable")
            coverage_id = (
                "coverage-"
                + deterministic_fingerprint(
                    {
                        "activity": activity.hydrated_source_id,
                        "transcriptomic": transcriptomic.hydrated_source_id,
                        "modality": modality,
                    }
                )[:24]
            )
            combinations.append(
                CombinationCoverage(
                    coverage_id=coverage_id,
                    activity_source_ids=[activity.hydrated_source_id],
                    transcriptomic_source_id=transcriptomic.hydrated_source_id,
                    requested_modality=modality,
                    identity_resolution_count=len(overlap),
                    activity_compound_count=len(activity_ids),
                    transcriptomic_compound_count=len(transcriptomic_ids),
                    overlap_count=len(overlap),
                    active_count=int(active) if isinstance(active, int) else None,
                    inactive_count=int(inactive) if isinstance(inactive, int) else None,
                    ambiguous_count=int(ambiguous) if isinstance(ambiguous, int) else None,
                    metadata_completeness={
                        field: 1.0 if context.get(field) else 0.0
                        for field in ("cell", "dose", "time")
                    },
                    context=context,
                    limitations=[
                        *activity.explicit_missing_fields,
                        *transcriptomic.explicit_missing_fields,
                    ],
                    computation_provenance=list(refs.values()),
                    activity_provider=activity.provider,
                    transcriptomic_provider=transcriptomic.provider,
                    cell_or_tissue_context=_context_values(context, "cell"),
                    dose_values=_context_values(context, "dose"),
                    exposure_times=_context_values(context, "time"),
                    stable_identifier_coverage=round(stable_coverage, 8),
                    identifier_resolution_losses=(
                        len(activity_ids - overlap) + len(transcriptomic_ids - overlap)
                    ),
                    activity_row_count=int(activity_summary.get("row_count", 0)),
                    signature_profile_count=int(transcriptomic_summary.get("profile_count", 0)),
                    class_balance={
                        key: float(value)
                        for key, value in {
                            "active": active,
                            "inactive": inactive,
                            "ambiguous": ambiguous,
                        }.items()
                    },
                    missing_metadata=missing,
                    quality_flags=quality_flags,
                    conflicting_label_count=_summary_value(
                        activity_summary, "conflicting_label_count"
                    ),
                    context_completeness=(
                        sum(bool(context.get(field)) for field in ("cell", "dose", "time")) / 3
                    ),
                    expected_assembled_sample_count=(
                        len(overlap_profiles) if overlap_profiles else len(overlap)
                    ),
                    expected_unique_compound_count=len(overlap),
                )
            )
    return CombinationCoverageSet(
        workflow_id=workflow_id,
        discovery_round=discovery_round,
        combinations=combinations,
    )


def _context_values(context: dict[str, Any], key: str) -> list[str]:
    value = context.get(key)
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return sorted({str(item) for item in values if str(item).strip()})


class SelectiveExpressionExtractionRequest(LifecycleContract):
    """Recipe-bound request for the existing post-approval LINCS slicing boundary."""

    workflow_id: str
    recipe_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature_ids_artifact: ArtifactReference
    approved_context: dict[str, Any]
    output_feature_space: str = "landmark_genes"


class SelectiveExpressionExtractionResult(LifecycleContract):
    expression_matrix: ArtifactReference
    selected_signature_ids: ArtifactReference
    gene_schema: ArtifactReference
    extraction_manifest: ArtifactReference


class PostApprovalExpressionExtractor(Protocol):
    """Production adapter implemented by the memory-bounded LINCS slice job."""

    def extract(
        self, request: SelectiveExpressionExtractionRequest
    ) -> SelectiveExpressionExtractionResult: ...


class LincsSelectiveExpressionExtractor:
    """Production adapter to the existing bounded local GCTX slicing implementation."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        staged_gctx_path: Path,
        gctx_release_manifest: ArtifactReference,
        landmark_gene_ids: list[str],
        batch_size: int = 256,
        rdcc_nbytes: int = 32 * 1024 * 1024,
        maximum_signature_ids: int = 50_000,
    ) -> None:
        self.artifact_store = artifact_store
        self.staged_gctx_path = Path(staged_gctx_path).resolve()
        self.gctx_release_manifest = gctx_release_manifest
        self.landmark_gene_ids = list(dict.fromkeys(landmark_gene_ids))
        self.batch_size = batch_size
        self.rdcc_nbytes = rdcc_nbytes
        self.maximum_signature_ids = maximum_signature_ids
        if not self.staged_gctx_path.is_file():
            raise ValueError("The reviewed local LINCS GCTX file is not staged.")
        if not self.landmark_gene_ids or len(self.landmark_gene_ids) > 20_000:
            raise ValueError("The reviewed landmark-gene set is empty or exceeds its bound.")
        if not 1 <= batch_size <= 1_024:
            raise ValueError("The GCTX signature batch must be between 1 and 1024.")
        if not 1_024 <= rdcc_nbytes <= 64 * 1024 * 1024:
            raise ValueError("The GCTX chunk cache must be between 1 KiB and 64 MiB.")
        if not 1 <= maximum_signature_ids <= 100_000:
            raise ValueError("The approved signature-selection bound is invalid.")

    @staticmethod
    def _reference(descriptor: Any) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )

    def extract(
        self, request: SelectiveExpressionExtractionRequest
    ) -> SelectiveExpressionExtractionResult:
        """Slice only approved signature IDs; never fetch or scan a remote matrix."""

        signature_descriptor, signature_content = self.artifact_store.get(
            request.signature_ids_artifact.artifact_id
        )
        release_descriptor, _ = self.artifact_store.get(self.gctx_release_manifest.artifact_id)
        if (
            signature_descriptor.workflow_id != request.workflow_id
            or signature_descriptor.sha256 != request.signature_ids_artifact.sha256
            or signature_descriptor.artifact_type != request.signature_ids_artifact.artifact_type
            or release_descriptor.workflow_id != request.workflow_id
            or release_descriptor.sha256 != self.gctx_release_manifest.sha256
            or release_descriptor.artifact_type != self.gctx_release_manifest.artifact_type
        ):
            raise ValueError("Expression extraction artifacts do not belong to this workflow.")
        try:
            payload = json.loads(signature_content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("The approved signature-ID artifact is not valid JSON.") from exc
        if isinstance(payload, dict):
            values = payload.get("signature_ids", payload.get("selected_signature_ids"))
        else:
            values = payload
        if not isinstance(values, list):
            raise ValueError("The approved signature-ID artifact must contain a JSON list.")
        if any(not isinstance(value, str) for value in values):
            raise ValueError("Approved signature IDs must be explicit strings.")
        signature_ids = list(dict.fromkeys(str(value).strip() for value in values))
        if (
            not signature_ids
            or any(not value for value in signature_ids)
            or any(len(value) > 200 for value in signature_ids)
            or len(signature_ids) > self.maximum_signature_ids
        ):
            raise ValueError("The approved signature-ID selection is empty or exceeds its bound.")

        from endoscan_jobs.gctx import slice_gctx_landmark

        matrix = slice_gctx_landmark(
            self.staged_gctx_path,
            row_ids=self.landmark_gene_ids,
            col_ids=signature_ids,
            batch_size=self.batch_size,
            rdcc_nbytes=self.rdcc_nbytes,
        )
        logical_prefix = f"{request.recipe_fingerprint[:16]}-lincs-selective"
        with tempfile.TemporaryDirectory(prefix="endoscan-lincs-slice-") as directory:
            output_path = Path(directory) / "expression.csv"
            matrix.T.rename_axis("signature_id").reset_index().to_csv(
                output_path, index=False, lineterminator="\n"
            )
            expression_descriptor = self.artifact_store.put_file(
                workflow_id=request.workflow_id,
                source_path=output_path,
                mime_type="text/csv",
                artifact_type="approved_expression_slice",
                logical_name=f"{logical_prefix}-expression.csv",
                producer="lincs-selective-expression-extractor",
                idempotency_key=f"{logical_prefix}:expression",
                original_source="reviewed-local-lincs-gctx",
                reviewed_maximum_bytes=500_000_000,
            )
        gene_descriptor = self.artifact_store.put_json(
            workflow_id=request.workflow_id,
            value={
                "gene_ids": self.landmark_gene_ids,
                "feature_space": request.output_feature_space,
            },
            artifact_type="gene_schema",
            logical_name=f"{logical_prefix}-gene-schema.json",
            producer="lincs-selective-expression-extractor",
            idempotency_key=f"{logical_prefix}:gene-schema",
        )
        manifest_descriptor = self.artifact_store.put_json(
            workflow_id=request.workflow_id,
            value={
                "recipe_fingerprint": request.recipe_fingerprint,
                "signature_ids_artifact": request.signature_ids_artifact.model_dump(mode="json"),
                "gctx_release_manifest": self.gctx_release_manifest.model_dump(mode="json"),
                "approved_context": request.approved_context,
                "signature_count": len(signature_ids),
                "gene_count": len(self.landmark_gene_ids),
                "batch_size": self.batch_size,
                "rdcc_nbytes": self.rdcc_nbytes,
                "network_requests": 0,
                "reader": "endoscan_jobs.gctx.slice_gctx_landmark",
            },
            artifact_type="selective_expression_extraction_manifest",
            logical_name=f"{logical_prefix}-manifest.json",
            producer="lincs-selective-expression-extractor",
            idempotency_key=f"{logical_prefix}:manifest",
        )
        return SelectiveExpressionExtractionResult(
            expression_matrix=self._reference(expression_descriptor),
            selected_signature_ids=request.signature_ids_artifact,
            gene_schema=self._reference(gene_descriptor),
            extraction_manifest=self._reference(manifest_descriptor),
        )


class OfflineAssemblyInput(LifecycleContract):
    fixture_id: str = Field(min_length=3, max_length=120)
    activity_rows: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)
    transcriptomic_rows: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)
    expression_rows: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)
    source_artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=500)
    network_requests: Literal[0] = 0
    provider_requests: Literal[0] = 0
    gctx_network_access: Literal[False] = False


class DatasetBundleManifest(LifecycleContract):
    workflow_id: str
    recipe_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    x_matrix: ArtifactReference
    y_labels: ArtifactReference
    sample_metadata: ArtifactReference
    compound_metadata: ArtifactReference
    selected_signature_ids: ArtifactReference
    gene_schema: ArtifactReference
    split_assignments: ArtifactReference
    excluded_unresolved_ledger: ArtifactReference
    raw_activity_records: ArtifactReference
    dataset_card: ArtifactReference
    provenance_manifest: ArtifactReference
    unique_compounds: int = Field(ge=1)
    total_profiles: int = Field(ge=1)
    large_data_inline: Literal[False] = False
    builder_mode: Literal["production_selective_extraction", "deterministic_offline_fixture"]


class DatasetQualityReport(LifecycleContract):
    workflow_id: str
    dataset_bundle_artifact: ArtifactReference
    unique_compounds: int = Field(ge=1)
    total_profiles: int = Field(ge=1)
    class_distribution: dict[str, int]
    context_distributions: dict[str, dict[str, int]]
    missing_values: int = Field(ge=0)
    identifier_resolution_losses: int = Field(ge=0)
    conflicts_and_exclusions: int = Field(ge=0)
    split_sizes: dict[str, int]
    compound_split_leakage: list[str] = Field(default_factory=list)
    leakage_check_passed: bool
    source_provenance: list[ArtifactReference] = Field(default_factory=list, max_length=500)
    quality_warnings: list[str] = Field(default_factory=list, max_length=100)
    review_status: Literal["awaiting_human_review"] = "awaiting_human_review"

    @model_validator(mode="after")
    def no_silent_leakage(self) -> DatasetQualityReport:
        if self.leakage_check_passed != (not self.compound_split_leakage):
            raise ValueError("Leakage status differs from the compound leakage evidence.")
        return self


class DatasetReviewDecision(LifecycleContract):
    workflow_id: str
    decision: Literal["approved_for_benchmarking", "rejected", "revision_requested"]
    reviewer_id: str
    dataset_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=3, max_length=4000)
    decided_at: datetime = Field(default_factory=utc_now)


class ExplorePublicationManifest(LifecycleContract):
    workflow_id: str
    compounds: ArtifactReference
    reference_signatures: ArtifactReference
    experimental_context: ArtifactReference
    endpoint_source_labels: ArtifactReference
    nearest_neighbor_index: ArtifactReference
    projection_coordinates: ArtifactReference
    user_facing_map: ArtifactReference
    support_matrix: ArtifactReference
    user_facing_manifest: ArtifactReference
    projection_method: Literal["umap_2d", "pca_2d_offline_fallback"]
    filters: list[str]
    recomputed_on_page_load: Literal[False] = False


class BenchmarkPlan(LifecycleContract):
    workflow_id: str
    candidate_models: list[
        Literal["logistic_regression", "random_forest", "hist_gradient_boosting"]
    ]
    grouped_by: Literal["compound_id"] = "compound_id"
    held_out_partition: Literal["test"] = "test"
    deterministic_seed: int = 17
    selection_policy: Literal["human_multi_metric_review"] = "human_multi_metric_review"
    preprocessing_fit_scope: Literal["training_only"] = "training_only"


class ModelBenchmarkResult(LifecycleContract):
    workflow_id: str
    candidate_id: str
    model_name: str
    model_artifact: ArtifactReference
    metrics: dict[str, Any]
    validation_predictions: ArtifactReference
    model_card_draft: ArtifactReference
    reproducibility_metadata: dict[str, Any]


class BenchmarkComparison(LifecycleContract):
    workflow_id: str
    candidate_results: list[ArtifactReference] = Field(min_length=3, max_length=20)
    metric_names: list[str] = Field(min_length=8)
    automatic_winner_selected: Literal[False] = False
    human_review_required: Literal[True] = True


class ModelSelectionRecord(LifecycleContract):
    workflow_id: str
    candidate_id: str
    reviewer_id: str
    decision_threshold: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=3, max_length=4000)
    approved_validation_plan: dict[str, Any]
    selected_at: datetime = Field(default_factory=utc_now)


class ModelReviewDecision(LifecycleContract):
    workflow_id: str
    decision: Literal["reject_all_models", "request_new_benchmark"]
    reviewer_id: str
    rationale: str = Field(min_length=3, max_length=4000)
    benchmark_comparison_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decided_at: datetime = Field(default_factory=utc_now)


class FinalValidationReport(LifecycleContract):
    workflow_id: str
    candidate_id: str
    held_out_metrics: dict[str, Any]
    feature_schema: ArtifactReference
    threshold_policy: ArtifactReference
    final_model: ArtifactReference
    final_model_card: ArtifactReference
    limitations: list[str] = Field(min_length=1, max_length=100)
    intended_use: str
    validation_status: ValidationStatusMatrix


class PublicationReceipt(LifecycleContract):
    workflow_id: str
    endpoint_id: str
    endpoint_version: str
    registry_entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_version_immutable: Literal[True] = True
    endpoint_version_immutable: Literal[True] = True
    model_library_visible: bool
    publication_approval_id: str
    published_at: datetime = Field(default_factory=utc_now)
