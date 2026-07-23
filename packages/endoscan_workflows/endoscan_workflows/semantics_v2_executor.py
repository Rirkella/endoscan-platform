"""Durable bridge from semantics-v2 discovery ledgers to typed provider tools.

The bridge deliberately stops before combination coverage. Provider adapters own
network access and normalization; this module only claims immutable ledger tasks,
dispatches their declared typed operation, and checkpoints complete candidate and
hydration metadata with content-addressed provenance.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any

from pydantic import Field
from sqlalchemy import select, update

from .artifacts import LocalArtifactStore
from .contracts import (
    ActorType,
    ArtifactDescriptor,
    ToolCallStatus,
    ToolInvocation,
    WorkflowState,
)
from .database import WorkflowDatabase
from .discovery_strategy import (
    ArtifactReference,
    CandidateStatus,
    DiscoveryExecutionLedger,
    DiscoveryExecutionRecord,
    DiscoveryPlan,
    DiscoveryTaskStatus,
    GlobalScientificSourceRequestAccounting,
    HydratedSource,
    HydratedSourceSet,
    HydrationCompleteness,
    ImmutableV2Contract,
    ProviderDiscoveryTask,
    SourceCandidate,
    SourceCandidateSet,
    validate_candidate_universe,
    validate_execution_ledger,
    validate_hydrated_sources,
)
from .errors import GuardNotSatisfied, StaleWorkflowVersion, WorkflowConflict
from .lincs_metadata import LincsMetadataRetrievalInput, LincsMetadataRetrievalOutput
from .lincs_streaming import LincsStreamingRetrievalOutput
from .models import ArtifactRow, EndpointBuildRow, TrainingDatasetWorkflowRow
from .preapproval_providers import (
    PROVIDER_TOOL_NAMES,
    DiskBackedHydratedSourceSet,
    DiskBackedSourceCandidateSet,
    ProviderMetadataExecutionOutput,
    ProviderNormalizedDatasetManifest,
    ToxCastPublicActivityCoverage,
    compile_provider_metadata_input,
)
from .provider_execution import ProviderExecutionOutcome
from .provider_result_materialization import compact_provider_dataset
from .repository import (
    append_event,
    canonical_json,
    deterministic_id,
    require_build,
    utc_text,
    versioned_payload,
)
from .source_governor import ScientificSourceRequestGovernor
from .tools import ToolRegistry

MAXIMUM_INLINE_DISCOVERY_RECORDS = 20_000
CID = re.compile(r"^CID:[1-9][0-9]{0,11}$")
TERMINAL_STATUSES = {
    DiscoveryTaskStatus.COMPLETED,
    DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES,
    DiscoveryTaskStatus.COMPLETED_WITH_PARTIAL_RESULTS,
    DiscoveryTaskStatus.FAILED,
    DiscoveryTaskStatus.BLOCKED,
    DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING,
}
PARTIAL_STATUSES = {DiscoveryTaskStatus.COMPLETED_WITH_PARTIAL_RESULTS}
FAILED_STATUSES = {DiscoveryTaskStatus.FAILED}
BLOCKED_STATUSES = {
    DiscoveryTaskStatus.BLOCKED,
    DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING,
}
PROVIDER_OPERATIONS = {
    **PROVIDER_TOOL_NAMES,
    "lincs-l1000": "retrieve_lincs_metadata_release",
}


class DiscoveryTaskMaterialization(ImmutableV2Contract):
    """Small durable bridge result; scientific tables remain in provider artifacts."""

    workflow_id: str
    discovery_round: int = Field(ge=0)
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str
    ledger_record: DiscoveryExecutionRecord
    candidates: list[SourceCandidate] = Field(default_factory=list, max_length=20_000)
    hydrated_sources: list[HydratedSource] = Field(default_factory=list, max_length=20_000)
    provider_dataset_artifacts: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )
    safe_failure_message: str | None = Field(default=None, max_length=1000)


class DiscoveryStageResult(ImmutableV2Contract):
    ledger: DiscoveryExecutionLedger
    ledger_artifact: ArtifactReference | None = None
    candidate_set: SourceCandidateSet | None = None
    candidate_artifact: ArtifactReference | None = None
    hydrated_set: HydratedSourceSet | None = None
    hydrated_artifact: ArtifactReference | None = None

    @property
    def artifact_references(self) -> list[ArtifactReference]:
        return [
            item
            for item in (
                self.ledger_artifact,
                self.candidate_artifact,
                self.hydrated_artifact,
            )
            if item is not None
        ]


def _document(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if isinstance(value.get("document"), dict):
        return value["document"]
    return value


def _reference(row: ArtifactRow | ArtifactDescriptor) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=row.id,
        sha256=row.sha256,
        artifact_type=row.artifact_type,
    )


def _deduplicated_references(values: list[ArtifactReference]) -> list[ArtifactReference]:
    return [
        value
        for _, value in sorted(
            {item.artifact_id: item for item in values}.items(), key=lambda item: item[0]
        )
    ]


class SemanticsV2DiscoveryExecutor:
    """Execute and checkpoint one immutable semantics-v2 provider ledger."""

    def __init__(
        self,
        database: WorkflowDatabase,
        artifacts: LocalArtifactStore,
        tool_registry: ToolRegistry,
        *,
        source_request_governor: ScientificSourceRequestGovernor | None = None,
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.tool_registry = tool_registry
        self.source_request_governor = source_request_governor

    @staticmethod
    def _task_result_name(round_number: int, task_id: str) -> str:
        return f"semantics-v2-task-result-round-{round_number}-{task_id}.json"

    @staticmethod
    def _provider_output_name(round_number: int, task_id: str) -> str:
        return f"semantics-v2-provider-output-round-{round_number}-{task_id}.json"

    @staticmethod
    def _compact_materialization_name(round_number: int, task_id: str) -> str:
        return f"semantics-v2-compact-materialization-round-{round_number}-{task_id}.json"

    def _load(self, workflow_id: str) -> tuple[DiscoveryPlan, DiscoveryExecutionLedger]:
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if (
                row is None
                or not row.discovery_plan_json
                or not row.discovery_execution_ledger_json
            ):
                raise GuardNotSatisfied("Semantics-v2 discovery plan and ledger are required.")
            plan = DiscoveryPlan.model_validate(_document(row.discovery_plan_json))
            ledger = DiscoveryExecutionLedger.model_validate(
                _document(row.discovery_execution_ledger_json)
            )
        validate_execution_ledger(plan, ledger)
        return plan, ledger

    def _artifact_references(
        self, workflow_id: str, artifact_ids: set[str]
    ) -> list[ArtifactReference]:
        if not artifact_ids:
            return []
        with self.database.session() as session:
            rows = session.scalars(
                select(ArtifactRow).where(ArtifactRow.id.in_(sorted(artifact_ids)))
            ).all()
        if {row.id for row in rows} != artifact_ids:
            raise WorkflowConflict("Provider inventory references a missing provenance artifact.")
        if any(row.workflow_id != workflow_id for row in rows):
            raise WorkflowConflict("Provider inventory references cross-workflow provenance.")
        return sorted((_reference(row) for row in rows), key=lambda item: item.artifact_id)

    def _checkpoint_ledger_record(
        self,
        workflow_id: str,
        *,
        task_id: str,
        expected_statuses: set[DiscoveryTaskStatus],
        record: DiscoveryExecutionRecord,
        actor: str,
    ) -> ArtifactReference:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if (
                row is None
                or not row.discovery_plan_json
                or not row.discovery_execution_ledger_json
            ):
                raise GuardNotSatisfied("Semantics-v2 discovery plan and ledger are required.")
            original_json = row.discovery_execution_ledger_json
            plan = DiscoveryPlan.model_validate(_document(row.discovery_plan_json))
            ledger = DiscoveryExecutionLedger.model_validate(_document(original_json))
            current = next((item for item in ledger.records if item.task_id == task_id), None)
            if current is None:
                raise WorkflowConflict("Discovery task disappeared from its immutable ledger.")
            if current.status not in expected_statuses:
                raise StaleWorkflowVersion("Concurrent discovery task update detected.")
            records = [record if item.task_id == task_id else item for item in ledger.records]
            updated = ledger.model_copy(update={"records": records})
            validate_execution_ledger(plan, updated)
            serialized = canonical_json(versioned_payload(document=updated.model_dump(mode="json")))
            result = session.execute(
                update(TrainingDatasetWorkflowRow)
                .where(
                    TrainingDatasetWorkflowRow.workflow_id == workflow_id,
                    TrainingDatasetWorkflowRow.discovery_execution_ledger_json == original_json,
                )
                .values(discovery_execution_ledger_json=serialized, updated_at=utc_text())
            )
            if getattr(result, "rowcount", 0) != 1:
                raise StaleWorkflowVersion("Concurrent discovery ledger claim detected.")
            artifact = self.artifacts._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(updated.model_dump(mode="json")).encode(),
                mime_type="application/json",
                artifact_type="discovery_execution_ledger",
                logical_name=(
                    f"discovery-ledger-round-{updated.discovery_round}-{task_id}-"
                    f"{record.status.value}.json"
                ),
                producer=actor,
                idempotency_key=(
                    f"semantics-v2-ledger:{updated.discovery_round}:{task_id}:"
                    f"{record.status.value}"
                ),
            )
            append_event(
                session,
                build,
                event_type="semantics_v2.discovery_task.checkpointed",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id=actor,
                idempotency_key=(
                    f"semantics-v2-task:{updated.discovery_round}:{task_id}:"
                    f"{record.status.value}:{artifact.sha256[:16]}"
                ),
                payload={
                    "task_id": task_id,
                    "status": record.status.value,
                    "ledger_artifact_id": artifact.id,
                    "ledger_sha256": artifact.sha256,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return _reference(artifact)

    def _persist_task_result(
        self, materialization: DiscoveryTaskMaterialization
    ) -> ArtifactReference:
        descriptor = self.artifacts.put_json(
            workflow_id=materialization.workflow_id,
            value=materialization.model_dump(mode="json"),
            artifact_type="semantics_v2_discovery_task_result",
            logical_name=self._task_result_name(
                materialization.discovery_round, materialization.task_id
            ),
            producer="semantics-v2-discovery-executor",
            idempotency_key=(
                f"semantics-v2-task-result:{materialization.discovery_round}:"
                f"{materialization.task_id}"
            ),
        )
        return ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )

    def _load_task_result(
        self, workflow_id: str, round_number: int, task_id: str
    ) -> tuple[DiscoveryTaskMaterialization, ArtifactReference] | None:
        descriptor = self.artifacts.find_by_logical_name(
            workflow_id, self._task_result_name(round_number, task_id)
        )
        if descriptor is None:
            return None
        verified, content = self.artifacts.get(descriptor.id)
        materialization = DiscoveryTaskMaterialization.model_validate_json(content)
        if materialization.workflow_id != workflow_id or materialization.task_id != task_id:
            raise WorkflowConflict("Discovery task result is not bound to the requested task.")
        return (
            materialization,
            ArtifactReference(
                artifact_id=verified.id,
                sha256=verified.sha256,
                artifact_type=verified.artifact_type,
            ),
        )

    def _persist_compact_materialization(
        self, materialization: DiscoveryTaskMaterialization
    ) -> ArtifactReference:
        """Persist the deterministic compact view without replacing row-level evidence."""

        descriptor = self.artifacts.put_json(
            workflow_id=materialization.workflow_id,
            value=materialization.model_dump(mode="json"),
            artifact_type="semantics_v2_compact_materialization_manifest",
            logical_name=self._compact_materialization_name(
                materialization.discovery_round, materialization.task_id
            ),
            producer="semantics-v2-discovery-executor",
            idempotency_key=(
                f"semantics-v2-compact-materialization:{materialization.discovery_round}:"
                f"{materialization.task_id}"
            ),
        )
        return ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )

    @staticmethod
    def _canonical_compact_materialization(
        materialization: DiscoveryTaskMaterialization,
    ) -> DiscoveryTaskMaterialization:
        compact_count = len({item.candidate_id for item in materialization.candidates})
        record = materialization.ledger_record.model_copy(
            update={
                "compact_source_candidate_count": compact_count,
                "unique_candidate_count": compact_count,
            }
        )
        return materialization.model_copy(update={"ledger_record": record})

    def _load_compact_materialization(
        self, workflow_id: str, round_number: int, task_id: str
    ) -> tuple[DiscoveryTaskMaterialization, ArtifactReference] | None:
        descriptor = self.artifacts.find_by_logical_name(
            workflow_id, self._compact_materialization_name(round_number, task_id)
        )
        if descriptor is None:
            return None
        verified, content = self.artifacts.get(descriptor.id)
        materialization = DiscoveryTaskMaterialization.model_validate_json(content)
        if (
            materialization.workflow_id != workflow_id
            or materialization.discovery_round != round_number
            or materialization.task_id != task_id
        ):
            raise WorkflowConflict("Compact materialization is not bound to the requested task.")
        if materialization != self._canonical_compact_materialization(materialization):
            raise WorkflowConflict("Compact materialization has inconsistent candidate accounting.")
        return materialization, _reference(verified)

    def _persist_provider_output(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        task: ProviderDiscoveryTask,
        output: dict[str, Any],
    ) -> ArtifactReference:
        descriptor = self.artifacts.put_json(
            workflow_id=workflow_id,
            value=output,
            artifact_type="semantics_v2_provider_execution_output",
            logical_name=self._provider_output_name(plan.discovery_round, task.task_id),
            producer="semantics-v2-discovery-executor",
            idempotency_key=(f"semantics-v2-provider-output:{plan.discovery_round}:{task.task_id}"),
        )
        return ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )

    def _load_provider_output(
        self, workflow_id: str, plan: DiscoveryPlan, task: ProviderDiscoveryTask
    ) -> tuple[dict[str, Any], ArtifactReference] | None:
        descriptor = self.artifacts.find_by_logical_name(
            workflow_id, self._provider_output_name(plan.discovery_round, task.task_id)
        )
        if descriptor is None:
            return None
        verified, content = self.artifacts.get(descriptor.id)
        value = json.loads(content)
        if not isinstance(value, dict):
            raise WorkflowConflict("Persisted provider execution output is not an object.")
        return value, ArtifactReference(
            artifact_id=verified.id,
            sha256=verified.sha256,
            artifact_type=verified.artifact_type,
        )

    def _legacy_provider_output(
        self,
        workflow_id: str,
        task: ProviderDiscoveryTask,
        running: DiscoveryExecutionRecord,
    ) -> tuple[dict[str, Any], list[ArtifactReference]] | None:
        """Reconstruct a pre-checkpoint provider output from immutable provider artifacts."""

        descriptors = self.artifacts.list_artifacts(workflow_id)
        if task.provider == "lincs-l1000":
            lincs_matches: list[tuple[dict[str, Any], Any]] = []
            for descriptor in descriptors:
                if descriptor.artifact_type != "semantics_v2_lincs_execution_output":
                    continue
                _verified, content = self.artifacts.get(descriptor.id)
                value = json.loads(content)
                if not isinstance(value, dict):
                    continue
                try:
                    if "normalization_manifest" in value:
                        lincs_execution = LincsStreamingRetrievalOutput.model_validate(
                            value
                        ).execution
                    else:
                        lincs_execution = LincsMetadataRetrievalOutput.model_validate(
                            value
                        ).execution
                except ValueError:
                    continue
                if lincs_execution.ledger_record.task_id == task.task_id:
                    lincs_matches.append((value, descriptor))
            if len(lincs_matches) != 1:
                return None
            value, descriptor = lincs_matches[0]
            return value, [_reference(descriptor)]

        normalized_matches: list[tuple[ProviderNormalizedDatasetManifest, Any]] = []
        for descriptor in descriptors:
            if descriptor.artifact_type != "preapproval_provider_normalization_manifest":
                continue
            _verified, content = self.artifacts.get(descriptor.id)
            try:
                manifest = ProviderNormalizedDatasetManifest.model_validate_json(content)
            except ValueError:
                continue
            if (
                manifest.provider == task.provider
                and manifest.completion_proof.task_id == task.task_id
            ):
                normalized_matches.append((manifest, descriptor))
        if len(normalized_matches) != 1:
            return None
        normalized, normalized_descriptor = normalized_matches[0]

        execution_matches: list[tuple[list[ProviderExecutionOutcome], Any]] = []
        for descriptor in descriptors:
            if descriptor.artifact_type != "preapproval_provider_execution_manifest":
                continue
            _verified, content = self.artifacts.get(descriptor.id)
            value = json.loads(content)
            if not isinstance(value, dict) or value.get("provider") != task.provider:
                continue
            try:
                executions = [
                    ProviderExecutionOutcome.model_validate(item)
                    for item in value.get("executions", [])
                ]
            except ValueError:
                continue
            if executions and any(
                item.ledger_record.task_id == task.task_id for item in executions
            ):
                execution_matches.append((executions, descriptor))
        if len(execution_matches) != 1:
            return None
        executions, execution_descriptor = execution_matches[0]

        toxcast_summary: ToxCastPublicActivityCoverage | None = None
        toxcast_reference: ArtifactReference | None = None
        if task.provider == "toxcast":
            coverage_matches: list[tuple[ToxCastPublicActivityCoverage, Any]] = []
            for descriptor in descriptors:
                if descriptor.artifact_type != "toxcast_public_activity_coverage":
                    continue
                _verified, content = self.artifacts.get(descriptor.id)
                try:
                    coverage = ToxCastPublicActivityCoverage.model_validate_json(content)
                except ValueError:
                    continue
                observed_modalities = set(coverage.modality_by_aeid.values())
                if task.modality and observed_modalities and observed_modalities != {task.modality}:
                    continue
                coverage_matches.append((coverage, descriptor))
            if len(coverage_matches) == 1:
                toxcast_summary, coverage_descriptor = coverage_matches[0]
                toxcast_reference = _reference(coverage_descriptor)

        provider_record = executions[-1].ledger_record.model_copy(
            update={
                "status": running.status,
                "completion_reason": normalized.completion_proof.reason,
                "error_classification": None,
                "source_response_artifacts": [_reference(execution_descriptor)],
            }
        )
        sqlite_reference = normalized.sqlite_artifact
        output = ProviderMetadataExecutionOutput(
            provider=task.provider,
            release_id=normalized.release_id,
            ledger_record=provider_record,
            execution_manifest_artifact=_reference(execution_descriptor),
            exact_execution_count=len(executions),
            page_or_file_execution_preview=executions[:20],
            execution_preview_is_complete=len(executions) <= 20,
            completion_proof=normalized.completion_proof,
            dataset_manifest=normalized,
            dataset_manifest_artifact=_reference(normalized_descriptor),
            candidates=DiskBackedSourceCandidateSet(
                artifact=sqlite_reference,
                exact_count=normalized.counts.source_candidates,
                preview=[],
                preview_is_complete=normalized.counts.source_candidates == 0,
            ),
            hydrated_sources=DiskBackedHydratedSourceSet(
                artifact=sqlite_reference,
                exact_count=normalized.counts.hydrated_sources,
                preview=[],
                preview_is_complete=normalized.counts.hydrated_sources == 0,
            ),
            normalization_ran=False,
            cache_only_replay=all(item.scientific_source_request_count == 0 for item in executions),
            access_mode=normalized.access_mode,
            capability_findings=normalized.capability_findings,
            toxcast_public_activity=toxcast_summary,
            toxcast_public_activity_artifact=toxcast_reference,
        )
        references = [
            _reference(execution_descriptor),
            _reference(normalized_descriptor),
            sqlite_reference,
        ]
        if toxcast_reference is not None:
            references.append(toxcast_reference)
        return output.model_dump(mode="json"), _deduplicated_references(references)

    def _provider_rows(
        self,
        workflow_id: str,
        task: ProviderDiscoveryTask,
        output: ProviderMetadataExecutionOutput,
    ) -> tuple[list[SourceCandidate], list[HydratedSource], list[ArtifactReference]]:
        dataset_refs = [output.execution_manifest_artifact]
        if output.dataset_manifest_artifact is not None:
            dataset_refs.append(output.dataset_manifest_artifact)
        if output.toxcast_public_activity_artifact is not None:
            dataset_refs.append(output.toxcast_public_activity_artifact)
        if output.candidates is None or output.hydrated_sources is None:
            if output.candidates is not None or output.hydrated_sources is not None:
                raise WorkflowConflict("Provider candidate and hydration outputs diverged.")
            return [], [], _deduplicated_references(dataset_refs)
        if output.candidates.artifact.artifact_id != output.hydrated_sources.artifact.artifact_id:
            raise WorkflowConflict("Provider candidate and hydration partitions diverged.")
        if output.dataset_manifest is None:
            raise WorkflowConflict("Provider dataset manifest is required for materialization.")
        descriptor, path = self.artifacts.verified_path(output.candidates.artifact.artifact_id)
        if descriptor.workflow_id != workflow_id:
            raise WorkflowConflict("Provider dataset artifact belongs to another workflow.")
        matched_source_identifiers: set[str] | None = None
        modality_by_source: dict[str, str] = {}
        provider_summary: dict[str, Any] = {}
        if output.toxcast_public_activity is not None:
            matched_source_identifiers = set(output.toxcast_public_activity.matched_aeids)
            modality_by_source = dict(output.toxcast_public_activity.modality_by_aeid)
            provider_summary = output.toxcast_public_activity.coverage.model_dump(mode="json")
        units = compact_provider_dataset(
            path,
            provider=task.provider,
            evidence_role=task.evidence_role,
            task_modality=task.modality,
            release_id=output.release_id,
            matched_source_identifiers=matched_source_identifiers,
            modality_by_source=modality_by_source,
            provider_summary=provider_summary,
        )
        if len(units) > MAXIMUM_INLINE_DISCOVERY_RECORDS:
            raise WorkflowConflict(
                "Provider candidate metadata exceeds the versioned SourceCandidateSet bound."
            )
        dataset_refs.append(output.candidates.artifact)
        raw_refs = self._artifact_references(
            workflow_id,
            {artifact_id for unit in units for artifact_id in unit.raw_artifact_ids},
        )
        raw_by_id = {item.artifact_id: item for item in raw_refs}
        candidates: list[SourceCandidate] = []
        hydrated: list[HydratedSource] = []
        source_version = output.dataset_manifest.source_version
        provenance = []
        if output.page_or_file_execution_preview:
            provenance = list(output.page_or_file_execution_preview[0].task.licence_and_provenance)
        compact_dataset_refs = _deduplicated_references(dataset_refs)
        for unit in units:
            candidate_id = deterministic_id(
                "candidate",
                workflow_id,
                task.provider,
                output.release_id,
                unit.evidence_role.value,
                unit.source_identifier,
                unit.modality or "",
            )
            unit_raw_refs = [
                raw_by_id[artifact_id]
                for artifact_id in unit.raw_artifact_ids
                if artifact_id in raw_by_id
            ]
            query_refs = _deduplicated_references(
                [*unit_raw_refs, output.execution_manifest_artifact]
            )
            candidates.append(
                SourceCandidate(
                    candidate_id=candidate_id,
                    provider=task.provider,
                    source_identifier=unit.source_identifier,
                    evidence_role=unit.evidence_role,
                    modality=unit.modality,
                    discovery_task_id=task.task_id,
                    contributing_task_ids=[task.task_id],
                    contributing_modalities=([unit.modality] if unit.modality else []),
                    release_id=output.release_id,
                    source_version=source_version,
                    discovery_query_provenance=query_refs,
                    provider_dataset_artifacts=compact_dataset_refs,
                    coverage_summary={"by_task": {task.task_id: unit.coverage_summary}},
                    relationship_summary={"by_task": {task.task_id: unit.relationship_summary}},
                    candidate_status=unit.candidate_status,
                    relevance_explanation=(
                        "Provider-normalized compact scientific source unit; row-level data "
                        "remain in referenced immutable artifacts and no source was selected."
                    ),
                )
            )
            hydrated.append(
                HydratedSource(
                    hydrated_source_id=deterministic_id("hydrated", workflow_id, candidate_id),
                    source_candidate_id=candidate_id,
                    provider=task.provider,
                    source_identifier=unit.source_identifier,
                    evidence_role=unit.evidence_role,
                    modality=unit.modality,
                    contributing_task_ids=[task.task_id],
                    contributing_modalities=([unit.modality] if unit.modality else []),
                    release_id=output.release_id,
                    verified_metadata={
                        **unit.verified_metadata,
                        "task_specific_findings": {
                            task.task_id: {
                                "coverage": unit.coverage_summary,
                                "relationships": unit.relationship_summary,
                            }
                        },
                    },
                    compound_index_available=unit.compound_index_available,
                    label_or_activity_fields_available=(unit.label_or_activity_fields_available),
                    organism=unit.organism,
                    experimental_context=unit.experimental_context,
                    source_version=source_version,
                    licence_and_provenance=provenance,
                    completeness_status=unit.completeness_status,
                    explicit_missing_fields=unit.explicit_missing_fields,
                    exclusion_reason=unit.exclusion_reason,
                    coverage_summary={"by_task": {task.task_id: unit.coverage_summary}},
                    relationship_summary={"by_task": {task.task_id: unit.relationship_summary}},
                    evidence_artifacts=_deduplicated_references(
                        [*unit_raw_refs, *compact_dataset_refs]
                    ),
                )
            )
        return candidates, hydrated, compact_dataset_refs

    def _lincs_rows(
        self,
        workflow_id: str,
        task: ProviderDiscoveryTask,
        output: LincsStreamingRetrievalOutput | LincsMetadataRetrievalOutput,
    ) -> tuple[
        DiscoveryExecutionRecord,
        list[SourceCandidate],
        list[HydratedSource],
        list[ArtifactReference],
    ]:
        execution = output.execution
        record = execution.ledger_record
        evidence = [
            item.raw_artifact
            for item in execution.page_or_file_results
            if item.raw_artifact is not None
        ]
        verified_metadata: dict[str, Any]
        context: dict[str, Any]
        source_version = execution.task.source_version
        complete = execution.completion_proof.completed
        if isinstance(output, LincsStreamingRetrievalOutput):
            manifest = output.normalization_manifest
            if manifest is None or output.normalization_manifest_artifact is None:
                return record, [], [], _deduplicated_references(evidence)
            evidence.extend(
                [
                    output.normalization_manifest_artifact,
                    manifest.index_manifest_artifact,
                    *(item.raw_artifact for item in manifest.role_artifacts),
                    *(item.normalized_artifact for item in manifest.role_artifacts),
                ]
            )
            verified_metadata = {
                "release_id": manifest.release_id,
                "source_release": manifest.source_release,
                "source_locator": manifest.source_locator,
                "bundle_fingerprint": manifest.bundle_fingerprint,
                "metadata_roles": {
                    item.logical_role: {
                        "rows": item.row_count,
                        "included": item.included_row_count,
                        "incomplete": item.incomplete_row_count,
                        "excluded": item.excluded_row_count,
                    }
                    for item in manifest.role_artifacts
                },
                "expression_values_retrieved": False,
                "transcriptomic_source_class": "primary_lincs_l1000",
                "stable_identity_bridge_available": (
                    manifest.role("pert_info").included_row_count > 0
                ),
                "expression_extraction_path": "registered_post_approval_level5_gctx",
                "joinability_eligible": (
                    manifest.role("pert_info").included_row_count > 0
                    and manifest.role("sig_info").included_row_count > 0
                ),
            }
            compound_index_available = manifest.role("pert_info").included_row_count > 0
            context = {
                "signature_metadata_rows": manifest.role("sig_info").included_row_count,
                "cell_metadata_rows": manifest.role("cell_info").included_row_count,
            }
        else:
            metadata = output.metadata
            if metadata is None:
                return record, [], [], _deduplicated_references(evidence)
            perturbagens_by_id = {
                item.pert_id: item for item in metadata.perturbagens if item.pert_id
            }
            measured_perturbagen_ids = {
                item.pert_id
                for item in metadata.signatures
                if item.pert_id and item.measured_chemical_perturbation
            }
            stable_compound_ids = sorted(
                {
                    value
                    for pert_id in measured_perturbagen_ids
                    if (perturbagen := perturbagens_by_id.get(pert_id)) is not None
                    for value in (
                        perturbagen.pubchem_cid,
                        perturbagen.inchikey,
                        perturbagen.pert_id,
                    )
                    if value
                }
            )
            verified_metadata = {
                "release_id": metadata.release_id,
                "source_release": metadata.source_release,
                "bundle_fingerprint": metadata.bundle_fingerprint,
                "perturbagen_count": len(metadata.perturbagens),
                "signature_count": len(metadata.signatures),
                "cell_count": len(metadata.cells),
                "gene_count": len(metadata.genes),
                "expression_values_retrieved": False,
                "transcriptomic_source_class": "primary_lincs_l1000",
                "stable_compound_ids": stable_compound_ids,
                "stable_identity_bridge_available": any(
                    (
                        perturbagens_by_id[pert_id].pubchem_cid
                        or perturbagens_by_id[pert_id].inchikey
                    )
                    for pert_id in measured_perturbagen_ids
                    if pert_id in perturbagens_by_id
                ),
                "expression_extraction_path": "registered_post_approval_level5_gctx",
                "joinability_eligible": bool(metadata.perturbagens and metadata.signatures),
            }
            compound_index_available = bool(metadata.perturbagens)
            context = {
                "signature_metadata_rows": len(metadata.signatures),
                "cell_metadata_rows": len(metadata.cells),
                "cell": sorted({item.cell_id for item in metadata.signatures if item.cell_id}),
                "dose": sorted({item.dose for item in metadata.signatures if item.dose}),
                "time": sorted(
                    {item.exposure_time for item in metadata.signatures if item.exposure_time}
                ),
            }
        if not complete:
            return record, [], [], _deduplicated_references(evidence)
        candidate_id = deterministic_id("candidate", workflow_id, task.task_id, task.provider)
        candidate = SourceCandidate(
            candidate_id=candidate_id,
            provider=task.provider,
            source_identifier=str(task.query_parameters["release_id"]),
            evidence_role=task.evidence_role,
            modality=task.modality,
            discovery_task_id=task.task_id,
            contributing_task_ids=[task.task_id],
            contributing_modalities=([task.modality] if task.modality else []),
            release_id=str(task.query_parameters["release_id"]),
            source_version=source_version,
            discovery_query_provenance=_deduplicated_references(evidence),
            provider_dataset_artifacts=_deduplicated_references(evidence),
            coverage_summary={
                "by_task": {
                    task.task_id: {
                        **context,
                        "compound_index_available": compound_index_available,
                    }
                }
            },
            candidate_status=CandidateStatus.METADATA_CANDIDATE,
            relevance_explanation=(
                "Complete reviewed LINCS metadata release; no expression values or final "
                "source selection."
            ),
        )
        hydrated = HydratedSource(
            hydrated_source_id=deterministic_id(
                "hydrated", workflow_id, task.task_id, task.provider
            ),
            source_candidate_id=candidate_id,
            provider=task.provider,
            source_identifier=candidate.source_identifier,
            evidence_role=task.evidence_role,
            modality=task.modality,
            contributing_task_ids=[task.task_id],
            contributing_modalities=([task.modality] if task.modality else []),
            release_id=str(task.query_parameters["release_id"]),
            verified_metadata=verified_metadata,
            compound_index_available=compound_index_available,
            label_or_activity_fields_available=False,
            organism=None,
            experimental_context=context,
            source_version=source_version,
            licence_and_provenance=list(execution.task.licence_and_provenance),
            completeness_status=HydrationCompleteness.COMPLETE,
            explicit_missing_fields=(
                []
                if verified_metadata["joinability_eligible"]
                else ["stable_perturbagen_index_or_signature_metadata"]
            ),
            exclusion_reason=(
                None
                if verified_metadata["joinability_eligible"]
                else "LINCS metadata lacks a joinable perturbagen/signature index."
            ),
            coverage_summary={
                "by_task": {
                    task.task_id: {
                        **context,
                        "profile_count": context["signature_metadata_rows"],
                    }
                }
            },
            evidence_artifacts=_deduplicated_references(evidence),
        )
        return record, [candidate], [hydrated], _deduplicated_references(evidence)

    def _upstream_identifiers(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
    ) -> tuple[ArtifactReference | None, int]:
        identifiers: set[str] = set()
        for task in plan.provider_specific_query_tasks:
            loaded = self._load_task_result(workflow_id, plan.discovery_round, task.task_id)
            if loaded is None:
                continue
            materialization, _ = loaded
            for reference in materialization.provider_dataset_artifacts:
                _, path = self.artifacts.verified_path(reference.artifact_id)
                with path.open("rb") as handle:
                    header = handle.read(16)
                if header != b"SQLite format 3\x00":
                    continue
                with sqlite3.connect(path) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "compound_index" in tables:
                        for cid, compound_identifier in connection.execute(
                            "SELECT cid, compound_identifier FROM compound_index"
                        ):
                            value = str(cid or compound_identifier or "")
                            if CID.fullmatch(value):
                                identifiers.add(value)
                    if "records" in tables:
                        columns = {
                            str(row[1]) for row in connection.execute("PRAGMA table_info(records)")
                        }
                        if "pubchem_cid" in columns:
                            for (cid,) in connection.execute(
                                "SELECT pubchem_cid FROM records WHERE pubchem_cid IS NOT NULL"
                            ):
                                if CID.fullmatch(str(cid)):
                                    identifiers.add(str(cid))
        ordered = sorted(identifiers, key=lambda value: int(value.removeprefix("CID:")))
        if not ordered:
            return None, 0
        content = ("\n".join(ordered) + "\n").encode()
        descriptor = self.artifacts.put_bytes(
            workflow_id=workflow_id,
            content=content,
            mime_type="text/plain",
            artifact_type="discovery_compound_identifier_set",
            logical_name=f"discovery-compound-identifiers-round-{plan.discovery_round}.jsonl",
            producer="semantics-v2-discovery-executor",
            idempotency_key=f"semantics-v2-identifiers:{plan.discovery_round}",
        )
        return (
            ArtifactReference(
                artifact_id=descriptor.id,
                sha256=descriptor.sha256,
                artifact_type=descriptor.artifact_type,
            ),
            len(ordered),
        )

    def _blocked_dependency(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        record: DiscoveryExecutionRecord,
    ) -> DiscoveryTaskMaterialization:
        terminal = record.model_copy(
            update={
                "status": DiscoveryTaskStatus.BLOCKED,
                "completion_reason": (
                    "No validated stable PubChem CID was available from completed upstream "
                    "metadata partitions; the identity provider was not invoked."
                ),
                "error_classification": "UPSTREAM_STABLE_COMPOUND_IDENTIFIERS_UNAVAILABLE",
            }
        )
        return DiscoveryTaskMaterialization(
            workflow_id=workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            task_id=record.task_id,
            ledger_record=terminal,
            safe_failure_message=terminal.completion_reason,
        )

    def _materialize_provider_output(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        task: ProviderDiscoveryTask,
        active_record: DiscoveryExecutionRecord,
        output_value: dict[str, Any],
        output_artifacts: list[ArtifactReference],
    ) -> DiscoveryTaskMaterialization:
        raw_record_count = 0
        normalized_record_count = 0
        provider_unique_record_count = 0
        if task.provider == "lincs-l1000":
            if "normalization_manifest" in output_value:
                lincs_output: LincsStreamingRetrievalOutput | LincsMetadataRetrievalOutput = (
                    LincsStreamingRetrievalOutput.model_validate(output_value)
                )
            else:
                lincs_output = LincsMetadataRetrievalOutput.model_validate(output_value)
            provider_record, candidates, hydrated, dataset_refs = self._lincs_rows(
                workflow_id, task, lincs_output
            )
            if isinstance(lincs_output, LincsStreamingRetrievalOutput):
                manifest = lincs_output.normalization_manifest
                if manifest is not None:
                    raw_record_count = sum(item.row_count for item in manifest.role_artifacts)
                    normalized_record_count = sum(
                        item.included_row_count for item in manifest.role_artifacts
                    )
                    provider_unique_record_count = manifest.role("sig_info").included_row_count
            elif lincs_output.metadata is not None:
                metadata = lincs_output.metadata
                raw_record_count = sum(
                    len(values)
                    for values in (
                        metadata.perturbagens,
                        metadata.signatures,
                        metadata.cells,
                        metadata.genes,
                    )
                )
                normalized_record_count = raw_record_count
                provider_unique_record_count = len(metadata.signatures)
            proof_complete = lincs_output.execution.completion_proof.completed
            partial_scientific_evidence = bool(candidates or hydrated) or any(
                result.raw_artifact is not None
                for result in lincs_output.execution.page_or_file_results
            )
            provider_failures = [
                result
                for result in lincs_output.execution.page_or_file_results
                if result.error_classification or result.safe_message
            ]
        else:
            provider_output = ProviderMetadataExecutionOutput.model_validate(output_value)
            provider_record = provider_output.ledger_record
            if provider_output.dataset_manifest is not None:
                counts = provider_output.dataset_manifest.counts
                raw_record_count = counts.raw_records
                normalized_record_count = sum(
                    (
                        counts.compound_mappings,
                        counts.activity_records,
                        counts.activity_summaries,
                        counts.transcriptomic_records,
                        counts.assay_relationships,
                        counts.assay_traversal_records,
                        counts.assay_annotations,
                        counts.release_inventory_records,
                        counts.publication_dataset_links,
                    )
                )
                role_counts = {
                    "activity": counts.activity_records,
                    "transcriptomic": counts.transcriptomic_records,
                    "identity": counts.compound_mappings,
                    "supporting_metadata": (
                        counts.release_inventory_records
                        + counts.publication_dataset_links
                        + counts.assay_annotations
                    ),
                }
                provider_unique_record_count = role_counts.get(
                    task.evidence_role.value, counts.source_candidates
                )
            candidates, hydrated, dataset_refs = self._provider_rows(
                workflow_id, task, provider_output
            )
            proof_complete = provider_output.completion_proof.completed
            partial_scientific_evidence = bool(candidates or hydrated) or any(
                result.raw_artifact is not None
                for execution in provider_output.page_or_file_execution_preview
                for result in execution.page_or_file_results
            )
            provider_failures = [
                result
                for execution in provider_output.page_or_file_execution_preview
                for result in execution.page_or_file_results
                if result.error_classification or result.safe_message
            ]
        provider_record = provider_record.model_copy(
            update={"retry_provenance": list(active_record.retry_provenance)}
        )
        dataset_refs = _deduplicated_references([*dataset_refs, *output_artifacts])
        if proof_complete:
            status = (
                DiscoveryTaskStatus.COMPLETED
                if candidates
                else DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES
            )
            error_classification = None
        elif partial_scientific_evidence:
            status = DiscoveryTaskStatus.COMPLETED_WITH_PARTIAL_RESULTS
            error_classification = (
                provider_record.error_classification or "partial_provider_retrieval"
            )
        else:
            status = DiscoveryTaskStatus.FAILED
            error_classification = (
                next(
                    (
                        item.error_classification
                        for item in provider_failures
                        if item.error_classification
                    ),
                    None,
                )
                or provider_record.error_classification
                or "provider_task_failed"
            )
        completion_reason = provider_record.completion_reason
        if status is DiscoveryTaskStatus.FAILED:
            completion_reason = next(
                (item.safe_message for item in provider_failures if item.safe_message),
                completion_reason,
            )
        terminal = provider_record.model_copy(
            update={
                "status": status,
                "search_result_count": len(candidates),
                "raw_record_count": raw_record_count,
                "normalized_record_count": normalized_record_count,
                "provider_unique_record_count": provider_unique_record_count,
                "compact_source_candidate_count": len(candidates),
                "retained_row_count": raw_record_count,
                "unique_candidate_count": len(candidates),
                "source_response_artifacts": dataset_refs,
                "row_level_artifact_references": dataset_refs if raw_record_count else [],
                "deterministic_summary_artifacts": output_artifacts,
                "error_classification": error_classification,
                "completion_reason": completion_reason,
            }
        )
        return DiscoveryTaskMaterialization(
            workflow_id=workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            task_id=task.task_id,
            ledger_record=terminal,
            candidates=candidates,
            hydrated_sources=hydrated,
            provider_dataset_artifacts=dataset_refs,
            safe_failure_message=(
                terminal.completion_reason if status in FAILED_STATUSES else None
            ),
        )

    def _post_provider_failure(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        record: DiscoveryExecutionRecord,
        *,
        phase: str,
        error: Exception,
        evidence: list[ArtifactReference],
    ) -> DiscoveryTaskMaterialization:
        safe_detail = str(error) if isinstance(error, WorkflowConflict) else None
        safe_message = safe_detail or (
            "Provider evidence was preserved, but compact discovery materialization failed."
        )
        status = (
            DiscoveryTaskStatus.COMPLETED_WITH_PARTIAL_RESULTS
            if evidence
            else DiscoveryTaskStatus.FAILED
        )
        terminal = record.model_copy(
            update={
                "status": status,
                "error_classification": f"{phase}_failed",
                "completion_reason": safe_message,
                "source_response_artifacts": _deduplicated_references(
                    [*record.source_response_artifacts, *evidence]
                ),
            }
        )
        return DiscoveryTaskMaterialization(
            workflow_id=workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            task_id=record.task_id,
            ledger_record=terminal,
            provider_dataset_artifacts=_deduplicated_references(evidence),
            safe_failure_message=safe_message,
        )

    def _invoke_task(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        task: ProviderDiscoveryTask,
        running: DiscoveryExecutionRecord,
    ) -> DiscoveryTaskMaterialization:
        expected_operation = PROVIDER_OPERATIONS.get(task.provider)
        if expected_operation != task.operation:
            raise WorkflowConflict(
                f"Discovery task operation is not registered for provider {task.provider}."
            )
        tool = self.tool_registry.get(task.operation)
        if tool.definition.input_schema_name != task.typed_input_contract:
            raise WorkflowConflict("Discovery task input contract differs from the typed tool.")
        allowed_output_contracts = {task.typed_output_contract}
        if task.provider == "lincs-l1000":
            allowed_output_contracts.add("LincsStreamingRetrievalOutput")
        if tool.definition.output_schema_name not in allowed_output_contracts:
            raise WorkflowConflict("Discovery task output contract differs from the typed tool.")
        if task.provider == "lincs-l1000":
            arguments = LincsMetadataRetrievalInput(
                release_id=str(task.query_parameters["release_id"]),
                ledger_record=running,
            ).model_dump(mode="json")
        else:
            upstream_artifact = None
            upstream_count = 0
            if task.provider == "pubchem-compound":
                upstream_artifact, upstream_count = self._upstream_identifiers(workflow_id, plan)
                if upstream_artifact is None:
                    return self._blocked_dependency(workflow_id, plan, running)
            arguments = compile_provider_metadata_input(
                task,
                running,
                upstream_identifier_artifact=upstream_artifact,
                upstream_identifier_count=upstream_count,
            ).model_dump(mode="json")
        result = self.tool_registry.invoke(
            ToolInvocation(
                tool_name=task.operation,
                arguments=arguments,
                workflow_id=workflow_id,
                workflow_stage=WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
                permission_scope=list(tool.definition.required_permissions),
                run_context={
                    "agent_role": "semantics-v2-discovery-executor",
                    "discovery_task_id": task.task_id,
                    "provider": task.provider,
                    "discovery_round": plan.discovery_round,
                    "maximum_global_scientific_source_requests": (
                        plan.scientific_and_execution_budgets.maximum_scientific_source_requests
                    ),
                },
                idempotency_key=(f"semantics-v2:{plan.discovery_round}:{task.task_id}:provider"),
            )
        )
        if result.status is not ToolCallStatus.COMPLETED or result.output is None:
            safe_message = (
                result.error.safe_message
                if result.error is not None
                else "Typed provider execution failed without a safe diagnostic."
            )
            failure_descriptor = self.artifacts.put_json(
                workflow_id=workflow_id,
                value=result.model_dump(mode="json"),
                artifact_type="semantics_v2_provider_tool_failure",
                logical_name=(
                    f"semantics-v2-provider-failure-round-{plan.discovery_round}-"
                    f"{task.task_id}.json"
                ),
                producer="semantics-v2-discovery-executor",
                idempotency_key=(
                    f"semantics-v2-provider-failure:{plan.discovery_round}:{task.task_id}"
                ),
            )
            failure_artifact = ArtifactReference(
                artifact_id=failure_descriptor.id,
                sha256=failure_descriptor.sha256,
                artifact_type=failure_descriptor.artifact_type,
            )
            terminal = running.model_copy(
                update={
                    "status": DiscoveryTaskStatus.FAILED,
                    "completion_reason": safe_message,
                    "error_classification": (
                        result.error.code if result.error is not None else "provider_tool_failed"
                    ),
                    "logical_tool_call_count": running.logical_tool_call_count + 1,
                }
            )
            return DiscoveryTaskMaterialization(
                workflow_id=workflow_id,
                discovery_round=plan.discovery_round,
                plan_fingerprint=plan.plan_fingerprint,
                task_id=task.task_id,
                ledger_record=terminal,
                provider_dataset_artifacts=[failure_artifact],
                safe_failure_message=safe_message,
            )
        output_artifact = self._persist_provider_output(workflow_id, plan, task, result.output)
        return self._materialize_provider_output(
            workflow_id,
            plan,
            task,
            running,
            result.output,
            [output_artifact],
        )

    def _terminalize(
        self,
        workflow_id: str,
        materialization: DiscoveryTaskMaterialization,
    ) -> ArtifactReference:
        record = materialization.ledger_record
        if self.source_request_governor is not None:
            accounting = self.source_request_governor.accounting(
                workflow_id,
                materialization.discovery_round,
                task_id=record.task_id,
            )
            if accounting is not None:
                record = record.model_copy(
                    update={
                        "scientific_source_request_count": accounting.reserved_requests,
                        "transport_attempt_count": accounting.reserved_requests,
                        "cache_hit_count": accounting.cache_hits,
                        "blocked_request_count": accounting.blocked_requests,
                        "requests_prevented_by_cancellation": (
                            accounting.prevented_by_cancellation
                        ),
                    }
                )
                materialization = materialization.model_copy(update={"ledger_record": record})
        result_artifact = self._persist_task_result(materialization)
        record = record.model_copy(
            update={
                "source_response_artifacts": _deduplicated_references(
                    [
                        *materialization.ledger_record.source_response_artifacts,
                        result_artifact,
                        *materialization.provider_dataset_artifacts,
                    ]
                )
            }
        )
        return self._checkpoint_ledger_record(
            workflow_id,
            task_id=record.task_id,
            expected_statuses={DiscoveryTaskStatus.RUNNING},
            record=record,
            actor="semantics-v2-discovery-executor",
        )

    def _materializations(
        self, workflow_id: str, plan: DiscoveryPlan, ledger: DiscoveryExecutionLedger
    ) -> list[DiscoveryTaskMaterialization]:
        values: list[DiscoveryTaskMaterialization] = []
        for record in ledger.records:
            compact = self._load_compact_materialization(
                workflow_id, plan.discovery_round, record.task_id
            )
            if compact is not None:
                materialization, _reference_value = compact
                if materialization.ledger_record.status != record.status:
                    raise WorkflowConflict("Compact materialization and execution ledger diverged.")
                values.append(materialization)
                continue
            loaded = self._load_task_result(workflow_id, plan.discovery_round, record.task_id)
            if loaded is None:
                if record.status is DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING:
                    continue
                if record.status in TERMINAL_STATUSES:
                    raise WorkflowConflict(
                        f"Terminal discovery task lacks materialized evidence: {record.task_id}."
                    )
                continue
            materialization, _ = loaded
            if materialization.ledger_record.status != record.status:
                raise WorkflowConflict("Task materialization and execution ledger diverged.")
            requires_compaction = any(
                not candidate.contributing_task_ids
                or candidate.release_id is None
                or not candidate.provider_dataset_artifacts
                for candidate in materialization.candidates
            ) or any(
                not source.contributing_task_ids or source.release_id is None
                for source in materialization.hydrated_sources
            )
            if requires_compaction:
                task = next(
                    item
                    for item in plan.provider_specific_query_tasks
                    if item.task_id == record.task_id
                )
                persisted_output = self._load_provider_output(workflow_id, plan, task)
                if persisted_output is not None:
                    output_value, output_reference = persisted_output
                    evidence = [loaded[1], output_reference]
                else:
                    legacy_output = self._legacy_provider_output(workflow_id, task, record)
                    if legacy_output is None:
                        raise WorkflowConflict(
                            "Legacy row-level task result lacks immutable provider evidence "
                            f"required for compact materialization: {record.task_id}."
                        )
                    output_value, legacy_references = legacy_output
                    evidence = [loaded[1], *legacy_references]
                materialization = self._materialize_provider_output(
                    workflow_id,
                    plan,
                    task,
                    record,
                    output_value,
                    _deduplicated_references(evidence),
                )
                if materialization.ledger_record.status != record.status:
                    raise WorkflowConflict(
                        "Compacted legacy task result differs from its terminal ledger status."
                    )
            materialization = self._canonical_compact_materialization(materialization)
            self._persist_compact_materialization(materialization)
            values.append(materialization)
        return values

    def _materialized_ledger(
        self,
        ledger: DiscoveryExecutionLedger,
        materializations: list[DiscoveryTaskMaterialization],
    ) -> DiscoveryExecutionLedger:
        """Reconcile legacy row-count ledgers with persisted compact task manifests."""

        by_task = {item.task_id: item.ledger_record for item in materializations}
        records: list[DiscoveryExecutionRecord] = []
        for current in ledger.records:
            materialized = by_task.get(current.task_id)
            if materialized is None:
                records.append(current)
                continue
            if materialized.status != current.status:
                raise WorkflowConflict("Materialized task status differs from its durable ledger.")
            records.append(
                current.model_copy(
                    update={
                        "raw_record_count": materialized.raw_record_count,
                        "normalized_record_count": materialized.normalized_record_count,
                        "provider_unique_record_count": (materialized.provider_unique_record_count),
                        "compact_source_candidate_count": (
                            materialized.compact_source_candidate_count
                        ),
                        "retained_row_count": materialized.retained_row_count,
                        "unique_candidate_count": materialized.unique_candidate_count,
                        "source_response_artifacts": _deduplicated_references(
                            [
                                *current.source_response_artifacts,
                                *materialized.source_response_artifacts,
                            ]
                        ),
                        "row_level_artifact_references": _deduplicated_references(
                            [
                                *current.row_level_artifact_references,
                                *materialized.row_level_artifact_references,
                            ]
                        ),
                        "deterministic_summary_artifacts": _deduplicated_references(
                            [
                                *current.deterministic_summary_artifacts,
                                *materialized.deterministic_summary_artifacts,
                            ]
                        ),
                    }
                )
            )
        global_accounting = ledger.global_request_accounting
        if self.source_request_governor is not None:
            accounting = self.source_request_governor.reconcile(
                ledger.workflow_id,
                ledger.discovery_round,
            )
            if accounting is not None:
                global_accounting = GlobalScientificSourceRequestAccounting(
                    allowed_global_request_budget=accounting.maximum_requests,
                    reserved_requests=accounting.reserved_requests,
                    completed_transport_attempts=accounting.completed_transport_attempts,
                    failed_transport_attempts=accounting.failed_transport_attempts,
                    cache_hits=accounting.cache_hits,
                    blocked_requests_after_budget_exhaustion=accounting.blocked_requests,
                    requests_prevented_by_cancellation=(accounting.prevented_by_cancellation),
                    remaining_requests=accounting.remaining_requests,
                )
        return ledger.model_copy(
            update={
                "records": records,
                "global_request_accounting": global_accounting,
            }
        )

    def _persist_materialized_ledger(
        self,
        workflow_id: str,
        plan: DiscoveryPlan,
        original: DiscoveryExecutionLedger,
        reconciled: DiscoveryExecutionLedger,
        *,
        compact_materialization_count: int,
    ) -> ArtifactReference | None:
        if reconciled == original:
            return None
        validate_execution_ledger(plan, reconciled)
        serialized = canonical_json(versioned_payload(document=reconciled.model_dump(mode="json")))
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.discovery_execution_ledger_json:
                raise GuardNotSatisfied("Semantics-v2 discovery ledger is required.")
            current = DiscoveryExecutionLedger.model_validate(
                _document(row.discovery_execution_ledger_json)
            )
            if current != original:
                raise StaleWorkflowVersion("Concurrent discovery ledger reconciliation detected.")
            original_json = row.discovery_execution_ledger_json
            result = session.execute(
                update(TrainingDatasetWorkflowRow)
                .where(
                    TrainingDatasetWorkflowRow.workflow_id == workflow_id,
                    TrainingDatasetWorkflowRow.discovery_execution_ledger_json == original_json,
                )
                .values(discovery_execution_ledger_json=serialized, updated_at=utc_text())
            )
            if getattr(result, "rowcount", 0) != 1:
                raise StaleWorkflowVersion("Concurrent discovery ledger reconciliation detected.")
            payload = reconciled.model_dump(mode="json")
            artifact = self.artifacts._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type="discovery_execution_ledger_reconciliation",
                logical_name=(
                    f"discovery-ledger-round-{reconciled.discovery_round}-"
                    "materialization-reconciled.json"
                ),
                producer="semantics-v2-discovery-executor",
                idempotency_key=(
                    f"semantics-v2-ledger-reconciliation:{reconciled.discovery_round}:"
                    f"{reconciled.plan_fingerprint[:16]}"
                ),
            )
            append_event(
                session,
                build,
                event_type="semantics_v2.discovery_ledger.materialization_reconciled",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id="semantics-v2-discovery-executor",
                idempotency_key=(
                    f"semantics-v2-ledger-reconciliation:{reconciled.discovery_round}:"
                    f"{artifact.sha256[:16]}"
                ),
                payload={
                    "ledger_artifact_id": artifact.id,
                    "ledger_sha256": artifact.sha256,
                    "compact_materialization_count": compact_materialization_count,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return _reference(artifact)

    @staticmethod
    def _status_ids(
        ledger: DiscoveryExecutionLedger, statuses: set[DiscoveryTaskStatus]
    ) -> list[str]:
        return sorted(item.task_id for item in ledger.records if item.status in statuses)

    @staticmethod
    def _missing_dimensions(
        ledger: DiscoveryExecutionLedger,
    ) -> list[str]:
        complete = {
            DiscoveryTaskStatus.COMPLETED,
            DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES,
        }
        return sorted(
            f"{item.provider}|{item.evidence_role.value}|{item.modality or 'none'}"
            for item in ledger.records
            if item.status not in complete
        )

    @staticmethod
    def _merge_task_summaries(values: list[dict[str, Any]], key: str = "by_task") -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for value in values:
            task_values = value.get(key, {})
            if isinstance(task_values, dict):
                merged.update(task_values)
        return {key: dict(sorted(merged.items()))}

    def _merge_candidates(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        grouped: dict[str, list[SourceCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.candidate_id, []).append(candidate)
        merged: list[SourceCandidate] = []
        for _candidate_id, values in sorted(grouped.items()):
            first = values[0]
            scientific_keys = {
                (
                    item.provider,
                    item.source_identifier,
                    item.evidence_role,
                    item.modality,
                    item.release_id,
                    item.source_version,
                )
                for item in values
            }
            if len(scientific_keys) != 1:
                raise WorkflowConflict("Canonical source candidate identity is inconsistent.")
            task_ids = sorted(
                {
                    task_id
                    for item in values
                    for task_id in (item.contributing_task_ids or [item.discovery_task_id])
                }
            )
            modalities = sorted(
                {
                    modality
                    for item in values
                    for modality in (
                        item.contributing_modalities
                        or ([item.modality] if item.modality is not None else [])
                    )
                    if isinstance(modality, str)
                }
            )
            merged.append(
                first.model_copy(
                    update={
                        "discovery_task_id": task_ids[0],
                        "contributing_task_ids": task_ids,
                        "contributing_modalities": modalities,
                        "discovery_query_provenance": _deduplicated_references(
                            [
                                reference
                                for item in values
                                for reference in item.discovery_query_provenance
                            ]
                        )[:50],
                        "provider_dataset_artifacts": _deduplicated_references(
                            [
                                reference
                                for item in values
                                for reference in item.provider_dataset_artifacts
                            ]
                        )[:50],
                        "coverage_summary": self._merge_task_summaries(
                            [item.coverage_summary for item in values]
                        ),
                        "relationship_summary": self._merge_task_summaries(
                            [item.relationship_summary for item in values]
                        ),
                    }
                )
            )
        return merged

    def _merge_hydrated_sources(self, sources: list[HydratedSource]) -> list[HydratedSource]:
        grouped: dict[str, list[HydratedSource]] = {}
        for source in sources:
            grouped.setdefault(source.hydrated_source_id, []).append(source)
        merged: list[HydratedSource] = []
        completeness_order = {
            HydrationCompleteness.COMPLETE: 0,
            HydrationCompleteness.PARTIAL: 1,
            HydrationCompleteness.UNAVAILABLE: 2,
            HydrationCompleteness.SCIENTIFICALLY_UNUSABLE: 3,
        }
        for _hydrated_id, values in sorted(grouped.items()):
            first = values[0]
            source_ids = {item.source_candidate_id for item in values}
            if len(source_ids) != 1:
                raise WorkflowConflict("Canonical hydrated source identity is inconsistent.")
            task_ids = sorted(
                {task_id for item in values for task_id in item.contributing_task_ids}
            )
            modalities = sorted(
                {
                    modality
                    for item in values
                    for modality in (
                        item.contributing_modalities
                        or ([item.modality] if item.modality is not None else [])
                    )
                    if isinstance(modality, str)
                }
            )
            task_findings: dict[str, Any] = {}
            for item in values:
                findings = item.verified_metadata.get("task_specific_findings", {})
                if isinstance(findings, dict):
                    task_findings.update(findings)
            verified_metadata = {
                **first.verified_metadata,
                "task_specific_findings": dict(sorted(task_findings.items())),
            }
            merged.append(
                first.model_copy(
                    update={
                        "contributing_task_ids": task_ids,
                        "contributing_modalities": modalities,
                        "verified_metadata": verified_metadata,
                        "compound_index_available": any(
                            item.compound_index_available for item in values
                        ),
                        "label_or_activity_fields_available": any(
                            item.label_or_activity_fields_available for item in values
                        ),
                        "licence_and_provenance": sorted(
                            {
                                provenance
                                for item in values
                                for provenance in item.licence_and_provenance
                            }
                        ),
                        "completeness_status": max(
                            (item.completeness_status for item in values),
                            key=lambda item: completeness_order[item],
                        ),
                        "explicit_missing_fields": sorted(
                            {field for item in values for field in item.explicit_missing_fields}
                        ),
                        "coverage_summary": self._merge_task_summaries(
                            [item.coverage_summary for item in values]
                        ),
                        "relationship_summary": self._merge_task_summaries(
                            [item.relationship_summary for item in values]
                        ),
                        "evidence_artifacts": _deduplicated_references(
                            [reference for item in values for reference in item.evidence_artifacts]
                        ),
                    }
                )
            )
        return merged

    def _persist_final_document(
        self,
        workflow_id: str,
        *,
        document: SourceCandidateSet | HydratedSourceSet,
        column: str,
        artifact_type: str,
        logical_name: str,
    ) -> ArtifactReference:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise GuardNotSatisfied("Training-dataset workflow state is missing.")
            payload = document.model_dump(mode="json")
            artifact = self.artifacts._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type=artifact_type,
                logical_name=logical_name,
                producer="semantics-v2-discovery-executor",
                idempotency_key=(f"semantics-v2-final:{document.discovery_round}:{artifact_type}"),
            )
            setattr(row, column, canonical_json(versioned_payload(document=payload)))
            row.updated_at = utc_text()
            append_event(
                session,
                build,
                event_type=f"semantics_v2.{artifact_type}.persisted",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id="semantics-v2-discovery-executor",
                idempotency_key=(
                    f"semantics-v2-final:{document.discovery_round}:{artifact_type}:"
                    f"{artifact.sha256[:16]}"
                ),
                payload={"artifact_id": artifact.id, "sha256": artifact.sha256},
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return _reference(artifact)

    def _candidate_set(
        self, workflow_id: str, plan: DiscoveryPlan, ledger: DiscoveryExecutionLedger
    ) -> tuple[
        SourceCandidateSet,
        ArtifactReference,
        DiscoveryExecutionLedger,
        ArtifactReference | None,
    ]:
        materializations = self._materializations(workflow_id, plan, ledger)
        materialized_ledger = self._materialized_ledger(ledger, materializations)
        ledger_artifact = self._persist_materialized_ledger(
            workflow_id,
            plan,
            ledger,
            materialized_ledger,
            compact_materialization_count=len(materializations),
        )
        candidates = self._merge_candidates(
            [candidate for item in materializations for candidate in item.candidates]
        )
        if len(candidates) > MAXIMUM_INLINE_DISCOVERY_RECORDS:
            raise WorkflowConflict(
                "Compacted source candidates exceed the versioned SourceCandidateSet bound."
            )
        dataset_refs = _deduplicated_references(
            [
                reference
                for item in materializations
                for reference in item.provider_dataset_artifacts
            ]
        )
        candidate_set = SourceCandidateSet(
            workflow_id=workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            candidates=candidates,
            all_planned_tasks_terminal=materialized_ledger.complete,
            complete_without_failures=not bool(
                self._status_ids(
                    materialized_ledger,
                    FAILED_STATUSES | PARTIAL_STATUSES | BLOCKED_STATUSES,
                )
            ),
            failed_task_ids=self._status_ids(materialized_ledger, FAILED_STATUSES),
            partial_task_ids=self._status_ids(materialized_ledger, PARTIAL_STATUSES),
            blocked_task_ids=self._status_ids(materialized_ledger, BLOCKED_STATUSES),
            completed_task_ids=self._status_ids(
                materialized_ledger, {DiscoveryTaskStatus.COMPLETED}
            ),
            completed_no_candidate_task_ids=self._status_ids(
                materialized_ledger, {DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES}
            ),
            running_task_ids=self._status_ids(materialized_ledger, {DiscoveryTaskStatus.RUNNING}),
            pending_task_ids=self._status_ids(materialized_ledger, {DiscoveryTaskStatus.PENDING}),
            missing_provider_modality_dimensions=self._missing_dimensions(materialized_ledger),
            provider_dataset_artifacts=dataset_refs,
            raw_record_count=sum(item.raw_record_count for item in materialized_ledger.records),
            normalized_record_count=sum(
                item.normalized_record_count for item in materialized_ledger.records
            ),
            provider_unique_record_count=sum(
                item.provider_unique_record_count for item in materialized_ledger.records
            ),
            compact_source_candidate_count=len(candidates),
            retained_row_count=sum(item.retained_row_count for item in materialized_ledger.records),
        )
        validate_candidate_universe(materialized_ledger, candidate_set)
        artifact = self._persist_final_document(
            workflow_id,
            document=candidate_set,
            column="source_candidates_json",
            artifact_type="source_candidates",
            logical_name=f"source-candidates-round-{plan.discovery_round}.json",
        )
        return candidate_set, artifact, materialized_ledger, ledger_artifact

    def execute_discovery(self, workflow_id: str) -> DiscoveryStageResult:
        last_ledger_artifact: ArtifactReference | None = None
        while True:
            plan, ledger = self._load(workflow_id)
            if any(item.status is DiscoveryTaskStatus.RUNNING for item in ledger.records):
                return DiscoveryStageResult(ledger=ledger, ledger_artifact=last_ledger_artifact)
            pending = next(
                (item for item in ledger.records if item.status is DiscoveryTaskStatus.PENDING),
                None,
            )
            if pending is None:
                if not ledger.complete:
                    raise WorkflowConflict("Discovery ledger has no executable or terminal task.")
                (
                    candidate_set,
                    candidate_artifact,
                    materialized_ledger,
                    reconciled_ledger_artifact,
                ) = self._candidate_set(workflow_id, plan, ledger)
                return DiscoveryStageResult(
                    ledger=materialized_ledger,
                    ledger_artifact=reconciled_ledger_artifact or last_ledger_artifact,
                    candidate_set=candidate_set,
                    candidate_artifact=candidate_artifact,
                )
            task = next(
                item
                for item in plan.provider_specific_query_tasks
                if item.task_id == pending.task_id
            )
            running = pending.model_copy(update={"status": DiscoveryTaskStatus.RUNNING})
            last_ledger_artifact = self._checkpoint_ledger_record(
                workflow_id,
                task_id=pending.task_id,
                expected_statuses={DiscoveryTaskStatus.PENDING},
                record=running,
                actor="semantics-v2-discovery-executor",
            )
            existing = self._load_task_result(workflow_id, plan.discovery_round, pending.task_id)
            evidence: list[ArtifactReference] = []
            try:
                if existing is not None:
                    materialization = existing[0]
                else:
                    persisted_output = self._load_provider_output(workflow_id, plan, task)
                    if persisted_output is not None:
                        output_value, output_reference = persisted_output
                        evidence = [output_reference]
                        materialization = self._materialize_provider_output(
                            workflow_id,
                            plan,
                            task,
                            running,
                            output_value,
                            evidence,
                        )
                    else:
                        legacy_output = self._legacy_provider_output(workflow_id, task, running)
                        if legacy_output is not None:
                            output_value, legacy_references = legacy_output
                            output_reference = self._persist_provider_output(
                                workflow_id, plan, task, output_value
                            )
                            evidence = _deduplicated_references(
                                [*legacy_references, output_reference]
                            )
                            materialization = self._materialize_provider_output(
                                workflow_id,
                                plan,
                                task,
                                running,
                                output_value,
                                evidence,
                            )
                        else:
                            materialization = self._invoke_task(workflow_id, plan, task, running)
                            evidence = materialization.provider_dataset_artifacts
            except StaleWorkflowVersion:
                raise
            except Exception as error:
                if not evidence:
                    persisted_output = self._load_provider_output(workflow_id, plan, task)
                    if persisted_output is not None:
                        evidence = [persisted_output[1]]
                    else:
                        legacy_output = self._legacy_provider_output(workflow_id, task, running)
                        if legacy_output is not None:
                            evidence = legacy_output[1]
                materialization = self._post_provider_failure(
                    workflow_id,
                    plan,
                    running,
                    phase="provider_result_materialization",
                    error=error,
                    evidence=evidence,
                )
            try:
                last_ledger_artifact = self._terminalize(workflow_id, materialization)
            except StaleWorkflowVersion:
                raise
            except Exception as error:
                _current_plan, current_ledger = self._load(workflow_id)
                current_record = next(
                    item for item in current_ledger.records if item.task_id == running.task_id
                )
                if current_record.status in TERMINAL_STATUSES:
                    raise
                fallback = self._post_provider_failure(
                    workflow_id,
                    plan,
                    running,
                    phase="task_terminalization",
                    error=error,
                    evidence=_deduplicated_references(
                        [*evidence, *materialization.provider_dataset_artifacts]
                    ),
                )
                last_ledger_artifact = self._terminalize(workflow_id, fallback)

    def execute_hydration(self, workflow_id: str) -> DiscoveryStageResult:
        plan, ledger = self._load(workflow_id)
        if not ledger.complete:
            raise GuardNotSatisfied("Every discovery task must be terminal before hydration.")
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.source_candidates_json:
                raise GuardNotSatisfied("SourceCandidateSet is required before hydration.")
            candidate_set = SourceCandidateSet.model_validate(_document(row.source_candidates_json))
        materializations = self._materializations(workflow_id, plan, ledger)
        hydrated = self._merge_hydrated_sources(
            [source for item in materializations for source in item.hydrated_sources]
        )
        dataset_refs = _deduplicated_references(
            [
                reference
                for item in materializations
                for reference in item.provider_dataset_artifacts
            ]
        )
        hydrated_set = HydratedSourceSet(
            workflow_id=workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            sources=hydrated,
            all_candidates_have_outcomes=True,
            complete_without_failures=candidate_set.complete_without_failures,
            failed_task_ids=candidate_set.failed_task_ids,
            partial_task_ids=candidate_set.partial_task_ids,
            blocked_task_ids=candidate_set.blocked_task_ids,
            completed_task_ids=candidate_set.completed_task_ids,
            completed_no_candidate_task_ids=(candidate_set.completed_no_candidate_task_ids),
            running_task_ids=candidate_set.running_task_ids,
            pending_task_ids=candidate_set.pending_task_ids,
            missing_provider_modality_dimensions=(
                candidate_set.missing_provider_modality_dimensions
            ),
            provider_dataset_artifacts=dataset_refs,
            hydrated_source_count=len(hydrated),
        )
        validate_hydrated_sources(candidate_set, hydrated_set)
        artifact = self._persist_final_document(
            workflow_id,
            document=hydrated_set,
            column="hydrated_sources_json",
            artifact_type="hydrated_sources",
            logical_name=f"hydrated-sources-round-{plan.discovery_round}.json",
        )
        return DiscoveryStageResult(
            ledger=ledger,
            candidate_set=candidate_set,
            hydrated_set=hydrated_set,
            hydrated_artifact=artifact,
        )

    def recover_interrupted(self) -> int:
        """Reset stale running ledger claims at process startup; provider caches are reused."""

        recovered = 0
        with self.database.session() as session:
            rows = session.scalars(select(TrainingDatasetWorkflowRow)).all()
            for row in rows:
                if not row.discovery_execution_ledger_json:
                    continue
                build = session.get(EndpointBuildRow, row.workflow_id)
                if (
                    build is None
                    or build.current_stage != WorkflowState.DISCOVERING_SOURCE_CANDIDATES.value
                ):
                    continue
                ledger = DiscoveryExecutionLedger.model_validate(
                    _document(row.discovery_execution_ledger_json)
                )
                running = [
                    item for item in ledger.records if item.status is DiscoveryTaskStatus.RUNNING
                ]
                if not running:
                    continue
                replacements = {
                    item.task_id: item.model_copy(
                        update={
                            "status": DiscoveryTaskStatus.PENDING,
                            "retry_provenance": [
                                *item.retry_provenance,
                                "resumed_from_interrupted_durable_claim",
                            ],
                        }
                    )
                    for item in running
                }
                updated = ledger.model_copy(
                    update={
                        "records": [replacements.get(item.task_id, item) for item in ledger.records]
                    }
                )
                row.discovery_execution_ledger_json = canonical_json(
                    versioned_payload(document=updated.model_dump(mode="json"))
                )
                row.updated_at = utc_text()
                artifact = self.artifacts._put_bytes(
                    session,
                    workflow_id=row.workflow_id,
                    content=canonical_json(updated.model_dump(mode="json")).encode(),
                    mime_type="application/json",
                    artifact_type="discovery_execution_ledger",
                    logical_name=(
                        f"discovery-ledger-round-{updated.discovery_round}-startup-recovery.json"
                    ),
                    producer="startup-recovery",
                    idempotency_key=(
                        f"semantics-v2-recovery-ledger:{updated.discovery_round}:"
                        + "-".join(sorted(replacements))
                    )[:160],
                )
                append_event(
                    session,
                    require_build(session, row.workflow_id),
                    event_type="semantics_v2.discovery_claims.recovered",
                    actor_type=ActorType.SYSTEM.value,
                    actor_id="startup-recovery",
                    idempotency_key=(
                        f"semantics-v2-recovery:{row.discovery_round}:"
                        + "-".join(sorted(replacements))
                    )[:160],
                    payload={
                        "task_ids": sorted(replacements),
                        "ledger_artifact_id": artifact.id,
                        "ledger_sha256": artifact.sha256,
                    },
                    from_state=build.current_stage,
                    to_state=build.current_stage,
                )
                recovered += len(replacements)
        return recovered

    def progress(self, workflow_id: str, stage: WorkflowState, version: int) -> dict[str, Any]:
        plan, ledger = self._load(workflow_id)
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            candidate_set = (
                SourceCandidateSet.model_validate(_document(row.source_candidates_json))
                if row is not None and row.source_candidates_json
                else None
            )
            hydrated_set = (
                HydratedSourceSet.model_validate(_document(row.hydrated_sources_json))
                if row is not None and row.hydrated_sources_json
                else None
            )
            latest_ledger_row = session.scalar(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type == "discovery_execution_ledger",
                )
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            )
        observed_statuses = Counter(item.status.value for item in ledger.records)
        statuses = {
            status.value: observed_statuses.get(status.value, 0) for status in DiscoveryTaskStatus
        }
        failures = [
            {
                "task_id": item.task_id,
                "error_classification": item.error_classification,
                "safe_message": item.completion_reason,
            }
            for item in ledger.records
            if item.status in FAILED_STATUSES | PARTIAL_STATUSES | BLOCKED_STATUSES
        ]
        artifact_refs = _deduplicated_references(
            [reference for item in ledger.records for reference in item.source_response_artifacts]
            + (candidate_set.provider_dataset_artifacts if candidate_set else [])
            + (hydrated_set.provider_dataset_artifacts if hydrated_set else [])
            + ([_reference(latest_ledger_row)] if latest_ledger_row is not None else [])
        )
        final_descriptors = [
            descriptor
            for descriptor in (
                self.artifacts.find_by_logical_name(
                    workflow_id, f"source-candidates-round-{plan.discovery_round}.json"
                ),
                self.artifacts.find_by_logical_name(
                    workflow_id, f"hydrated-sources-round-{plan.discovery_round}.json"
                ),
            )
            if descriptor is not None
        ]
        artifact_refs = _deduplicated_references(
            [
                *artifact_refs,
                *[
                    ArtifactReference(
                        artifact_id=item.id,
                        sha256=item.sha256,
                        artifact_type=item.artifact_type,
                    )
                    for item in final_descriptors
                ],
            ]
        )
        return {
            "schema_version": "1.0.0",
            "current_stage": stage.value,
            "build_version": version,
            "discovery_round": plan.discovery_round,
            "plan_fingerprint": plan.plan_fingerprint,
            "ledger_task_total": len(ledger.records),
            "ledger_task_totals_by_status": dict(sorted(statuses.items())),
            "candidate_count": len(candidate_set.candidates) if candidate_set else 0,
            "hydrated_source_count": len(hydrated_set.sources) if hydrated_set else 0,
            "completed_task_count": statuses[DiscoveryTaskStatus.COMPLETED.value],
            "completed_no_candidate_task_count": statuses[
                DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES.value
            ],
            "partial_task_count": len(self._status_ids(ledger, PARTIAL_STATUSES)),
            "failed_task_count": len(self._status_ids(ledger, FAILED_STATUSES)),
            "blocked_task_count": len(self._status_ids(ledger, BLOCKED_STATUSES)),
            "running_task_count": statuses[DiscoveryTaskStatus.RUNNING.value],
            "pending_task_count": statuses[DiscoveryTaskStatus.PENDING.value],
            "artifact_references": [item.model_dump(mode="json") for item in artifact_refs],
            "safe_failure_summary": failures,
        }
