from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from endoscan_jobs.gctx import write_synthetic_gctx
from pydantic import ValidationError
from sqlalchemy import select

from endoscan_core.endpoint_building import assemble_approved_dataset
from endoscan_workflows.contracts import (
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    IdempotencyClassification,
    SideEffectClassification,
    ToolAccessPhase,
    ToolCallStatus,
    ToolDefinition,
    ToolInvocation,
    TransitionRequest,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.discovery_strategy import (
    WORKFLOW_SEMANTICS_V2,
    ArtifactReference,
    CapabilityAvailability,
    CapabilityPhase,
    CombinationCoverage,
    CombinationCoverageSet,
    DiscoveryExecutionLedger,
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    EvidenceRole,
    HydratedSource,
    HydratedSourceSet,
    HydrationCompleteness,
    OperationalCapability,
    OperationalCapabilityRegistry,
    ProposalStatus,
    ProviderCapabilityDeclaration,
    ProviderOperationalProfile,
    SourceCandidate,
    SourceCandidateSet,
    StrategyProposal,
    StrategyProposalSet,
    load_operational_capability_registry,
    validate_candidate_universe,
)
from endoscan_workflows.endpoint_lifecycle import (
    DeterministicAssemblyStrategyAgent,
    LincsSelectiveExpressionExtractor,
    SelectiveExpressionExtractionRequest,
    StrategyAgentInput,
    calculate_combination_coverage,
)
from endoscan_workflows.errors import GuardNotSatisfied, InvalidTransition, WorkflowConflict
from endoscan_workflows.models import EndpointBuildRow, TrainingDatasetWorkflowRow
from endoscan_workflows.offline_demo import (
    build_offline_assembly_input,
    execute_offline_demo,
    load_offline_demo_definition,
)
from endoscan_workflows.tools import RegisteredTool, ToolInput, ToolOutput, ToolRegistry

FIXTURES = Path(__file__).resolve().parents[1] / "endoscan_workflows" / "fixtures"


def test_dataset_builder_rejects_sources_outside_approved_recipe() -> None:
    fixture = build_offline_assembly_input("dna_damage")
    transcriptomic_source = str(fixture.transcriptomic_rows[0]["provider"])

    with pytest.raises(ValueError, match="source access not approved"):
        assemble_approved_dataset(
            activity_rows=fixture.activity_rows,
            transcriptomic_rows=fixture.transcriptomic_rows,
            expression_rows=fixture.expression_rows,
            approved_activity_sources={"unapproved-activity-source"},
            approved_transcriptomic_sources={transcriptomic_source},
            approved_modalities={"pathway_activation"},
            approved_label_policy={"operator": "source_backed_activity_call"},
            approved_context_filters={},
            exclusion_rules=[],
        )


def test_production_lincs_adapter_uses_bounded_local_gctx_slicer(
    workflow_runtime, tmp_path
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Local selective LINCS extraction",
            endpoint_slug="local-selective-lincs",
            biological_goal="Verify recipe-bound local expression slicing.",
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            idempotency_key="local-selective-lincs",
        )
    )
    signature_descriptor = store.put_json(
        workflow_id=build.id,
        value={"signature_ids": ["SIG_A", "SIG_C"]},
        artifact_type="approved_signature_ids",
        logical_name="approved-signature-ids.json",
        producer="test",
        idempotency_key="local-selective-lincs:signatures",
    )
    release_descriptor = store.put_json(
        workflow_id=build.id,
        value={"release": "synthetic-lincs", "reviewed": True},
        artifact_type="lincs_release_manifest",
        logical_name="lincs-release.json",
        producer="test",
        idempotency_key="local-selective-lincs:release",
    )
    gctx_path = tmp_path / "synthetic.gctx"
    write_synthetic_gctx(
        gctx_path,
        pd.DataFrame(
            {
                "SIG_A": [1.0, 2.0, 3.0, 4.0],
                "SIG_B": [5.0, 6.0, 7.0, 8.0],
                "SIG_C": [9.0, 10.0, 11.0, 12.0],
            },
            index=["GENE_1", "GENE_2", "GENE_3", "GENE_4"],
        ),
    )
    signature_reference = ArtifactReference(
        artifact_id=signature_descriptor.id,
        sha256=signature_descriptor.sha256,
        artifact_type=signature_descriptor.artifact_type,
    )
    release_reference = ArtifactReference(
        artifact_id=release_descriptor.id,
        sha256=release_descriptor.sha256,
        artifact_type=release_descriptor.artifact_type,
    )
    extractor = LincsSelectiveExpressionExtractor(
        artifact_store=store,
        staged_gctx_path=gctx_path,
        gctx_release_manifest=release_reference,
        landmark_gene_ids=["GENE_1", "GENE_3"],
        batch_size=1,
        rdcc_nbytes=1_024,
    )

    result = extractor.extract(
        SelectiveExpressionExtractionRequest(
            workflow_id=build.id,
            recipe_fingerprint="a" * 64,
            signature_ids_artifact=signature_reference,
            approved_context={"cell": ["CELL_A"]},
        )
    )

    _descriptor, matrix_content = store.get(result.expression_matrix.artifact_id)
    matrix = pd.read_csv(io.BytesIO(matrix_content))
    assert list(matrix["signature_id"]) == ["SIG_A", "SIG_C"]
    assert list(matrix.columns) == ["signature_id", "GENE_1", "GENE_3"]
    _descriptor, manifest_content = store.get(result.extraction_manifest.artifact_id)
    manifest = json.loads(manifest_content)
    assert manifest["reader"] == "endoscan_jobs.gctx.slice_gctx_landmark"
    assert manifest["network_requests"] == 0
    assert manifest["signature_count"] == 2


def _capability(
    capability: OperationalCapability,
    *,
    availability: CapabilityAvailability = CapabilityAvailability.OPERATIONAL,
) -> ProviderCapabilityDeclaration:
    return ProviderCapabilityDeclaration(
        capability=capability,
        availability=availability,
        phase=CapabilityPhase.PRE_APPROVAL,
        typed_tool_or_adapter_method=f"fixture.{capability.value}",
        input_contract=f"{capability.value}.input",
        output_contract=f"{capability.value}.output",
        evidence="Offline fixture capability declaration.",
    )


def _profile(
    provider: str,
    role: EvidenceRole,
    capabilities: list[OperationalCapability],
    *,
    missing: OperationalCapability | None = None,
) -> ProviderOperationalProfile:
    capabilities = list(
        dict.fromkeys(
            [
                *capabilities,
                OperationalCapability.CACHE_REPLAY,
                OperationalCapability.VERSION_CHECKSUM_PROVENANCE,
            ]
        )
    )
    return ProviderOperationalProfile(
        provider=provider,
        display_name=provider.replace("-", " ").title(),
        registered_source_id=provider,
        evidence_roles=[role],
        supported_modalities=["*"],
        capabilities=[
            _capability(
                item,
                availability=(
                    CapabilityAvailability.MISSING
                    if item is missing
                    else CapabilityAvailability.OPERATIONAL
                ),
            )
            for item in capabilities
        ],
    )


def _fixture_registry() -> OperationalCapabilityRegistry:
    activity = [
        OperationalCapability.CANDIDATE_SEARCH,
        OperationalCapability.PAGINATION,
        OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
        OperationalCapability.ACTIVITY_ASSAY_CATALOGUE,
        OperationalCapability.COMPOUND_IDENTIFIER_INDEX,
        OperationalCapability.ACTIVITY_CALL_RETRIEVAL,
        OperationalCapability.RELATED_ASSAY_RELATIONSHIPS,
    ]
    transcriptomic = [
        OperationalCapability.CANDIDATE_SEARCH,
        OperationalCapability.PAGINATION,
        OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
        OperationalCapability.CONTEXT_METADATA,
        OperationalCapability.COMPOUND_SIGNATURE_OVERLAP,
    ]
    return OperationalCapabilityRegistry(
        providers=[
            _profile("activity-alpha", EvidenceRole.ACTIVITY, activity),
            _profile(
                "activity-beta",
                EvidenceRole.ACTIVITY,
                activity,
                missing=OperationalCapability.RELATED_ASSAY_RELATIONSHIPS,
            ),
            _profile("transcript-alpha", EvidenceRole.TRANSCRIPTOMIC, transcriptomic),
            _profile("transcript-beta", EvidenceRole.TRANSCRIPTOMIC, transcriptomic),
            _profile(
                "identity-alpha",
                EvidenceRole.IDENTITY,
                [
                    OperationalCapability.COMPOUND_IDENTIFIER_INDEX,
                    OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
                ],
            ),
            _profile(
                "supporting-alpha",
                EvidenceRole.SUPPORTING_METADATA,
                [OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE],
            ),
        ]
    )


def _write_fixture_registry(service, tmp_path: Path) -> None:
    registry_path = tmp_path / "registry" / "data"
    registry_path.mkdir(parents=True)
    (registry_path / "operational_provider_capabilities.json").write_text(
        _fixture_registry().model_dump_json(indent=2), encoding="utf-8"
    )
    service.repo_root = tmp_path


def _start_approved_broad_build(service):
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Example receptor binding agonism antagonism",
            endpoint_slug="example-receptor-broad-v2",
            biological_goal=(
                "Construct a compound-level public training dataset linking endpoint activity "
                "to compound-induced transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="semantics-v2-build",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=build.version,
        actor="test-admin",
        idempotency_key="semantics-v2-start",
    )
    approval = service.list_approvals(build.id, pending_only=True)[0]
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key="semantics-v2-spec-approved",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    assert approved.current_stage is WorkflowState.APPROVED_SPECIFICATION
    return approved


def _start_approved_fixture_build(service, fixture_name: str):
    definition = load_offline_demo_definition(fixture_name)
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name=definition["endpoint_name"],
            endpoint_slug=definition["endpoint_slug"],
            biological_goal=definition["biological_goal"],
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key=f"stage2-{fixture_name}-build",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=build.version,
        actor="test-admin",
        idempotency_key=f"stage2-{fixture_name}-start",
    )
    approval = service.list_approvals(build.id, pending_only=True)[0]
    return service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key=f"stage2-{fixture_name}-spec-approved",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )


def _review_documents(service, workflow_id: str):
    fixture = json.loads((FIXTURES / "semantics_v2_multi_provider.json").read_text())
    workflow = service.training_dataset_workflow(workflow_id)
    plan = workflow["discovery_plan"]
    task_lookup = {
        (item["provider"], item.get("modality"), item["evidence_role"]): item["task_id"]
        for item in plan["provider_specific_query_tasks"]
    }
    with service.database.session() as session:
        persisted_raw = service.artifact_store._put_bytes(
            session,
            workflow_id=workflow_id,
            content=json.dumps(fixture, sort_keys=True).encode(),
            mime_type="application/json",
            artifact_type="offline_fixture_source_response",
            logical_name="semantics-v2-offline-source-fixture.json",
            producer="offline-fixture-orchestrator",
            idempotency_key="semantics-v2-offline-source-fixture",
        )
    raw_artifact = ArtifactReference(
        artifact_id=persisted_raw.id,
        sha256=persisted_raw.sha256,
        artifact_type=persisted_raw.artifact_type,
    )
    candidates = []
    source_compounds: dict[str, set[str]] = {}
    for source in fixture["activity_sources"]:
        source_compounds[source["source_id"]] = set(source["compounds"])
        candidates.append(
            SourceCandidate(
                candidate_id=f"candidate-{source['source_id']}",
                provider=source["provider"],
                source_identifier=source["source_id"],
                evidence_role=EvidenceRole.ACTIVITY,
                modality=source["modality"],
                discovery_task_id=task_lookup[
                    (source["provider"], source["modality"], EvidenceRole.ACTIVITY.value)
                ],
                discovery_query_provenance=[raw_artifact],
                candidate_status="metadata_candidate",
                descriptive_rank=1,
                relevance_explanation="Optional descriptive rank; it does not select this source.",
            )
        )
    for source in fixture["transcriptomic_sources"]:
        source_compounds[source["source_id"]] = set(source["compounds"])
        candidates.append(
            SourceCandidate(
                candidate_id=f"candidate-{source['source_id']}",
                provider=source["provider"],
                source_identifier=source["source_id"],
                evidence_role=EvidenceRole.TRANSCRIPTOMIC,
                modality=None,
                discovery_task_id=task_lookup[
                    (source["provider"], None, EvidenceRole.TRANSCRIPTOMIC.value)
                ],
                discovery_query_provenance=[raw_artifact],
                candidate_status="metadata_candidate",
            )
        )
    records = []
    candidates_by_task: dict[str, int] = {}
    for candidate in candidates:
        candidates_by_task[candidate.discovery_task_id] = (
            candidates_by_task.get(candidate.discovery_task_id, 0) + 1
        )
    for item in plan["provider_specific_query_tasks"]:
        blocked = item["provider"] == fixture["missing_provider_capability"]["provider"]
        count = candidates_by_task.get(item["task_id"], 0)
        records.append(
            DiscoveryExecutionRecord(
                task_id=item["task_id"],
                provider=item["provider"],
                evidence_role=item["evidence_role"],
                modality=item.get("modality"),
                status=(
                    DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING
                    if blocked
                    else DiscoveryTaskStatus.COMPLETED
                    if count
                    else DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES
                ),
                pages_or_cursors_attempted=["fixture-page-1"] if not blocked else [],
                search_result_count=count,
                unique_candidate_count=count,
                source_response_artifacts=[raw_artifact] if not blocked else [],
                completion_reason=(
                    "Fixture catalogue exhausted."
                    if not blocked
                    else "Required provider capability is missing."
                ),
                error_classification=(
                    "REGISTERED_PROVIDER_CAPABILITY_MISSING" if blocked else None
                ),
            )
        )
    ledger = DiscoveryExecutionLedger(
        workflow_id=workflow_id,
        discovery_round=0,
        plan_fingerprint=plan["plan_fingerprint"],
        records=records,
    )
    candidate_set = SourceCandidateSet(
        workflow_id=workflow_id,
        discovery_round=0,
        plan_fingerprint=plan["plan_fingerprint"],
        candidates=candidates,
    )
    hydrated = []
    for candidate in candidates:
        source = next(
            item
            for item in [*fixture["activity_sources"], *fixture["transcriptomic_sources"]]
            if item["source_id"] == candidate.source_identifier
        )
        hydrated.append(
            HydratedSource(
                hydrated_source_id=f"hydrated-{candidate.source_identifier}",
                source_candidate_id=candidate.candidate_id,
                provider=candidate.provider,
                source_identifier=candidate.source_identifier,
                evidence_role=candidate.evidence_role,
                modality=candidate.modality,
                verified_metadata={"compound_ids": source["compounds"]},
                compound_index_available=True,
                label_or_activity_fields_available=(
                    candidate.evidence_role is EvidenceRole.ACTIVITY
                ),
                organism="Homo sapiens",
                experimental_context=source.get("context", {}),
                source_version="fixture-v1",
                licence_and_provenance=["offline test fixture"],
                completeness_status=HydrationCompleteness.COMPLETE,
                evidence_artifacts=[raw_artifact],
            )
        )
    hydrated_set = HydratedSourceSet(
        workflow_id=workflow_id,
        discovery_round=0,
        sources=hydrated,
    )
    activity = [item for item in hydrated if item.evidence_role is EvidenceRole.ACTIVITY]
    transcriptomic = [
        item for item in hydrated if item.evidence_role is EvidenceRole.TRANSCRIPTOMIC
    ]
    coverage = []
    for activity_source in activity:
        source_record = next(
            item
            for item in fixture["activity_sources"]
            if item["source_id"] == activity_source.source_identifier
        )
        for transcript_source in transcriptomic:
            overlap = (
                source_compounds[activity_source.source_identifier]
                & source_compounds[transcript_source.source_identifier]
            )
            coverage.append(
                CombinationCoverage(
                    coverage_id=(
                        f"coverage-{activity_source.source_identifier}-"
                        f"{transcript_source.source_identifier}"
                    ),
                    activity_source_ids=[activity_source.hydrated_source_id],
                    transcriptomic_source_id=transcript_source.hydrated_source_id,
                    requested_modality=str(activity_source.modality),
                    identity_resolution_count=len(overlap),
                    activity_compound_count=len(
                        source_compounds[activity_source.source_identifier]
                    ),
                    transcriptomic_compound_count=len(
                        source_compounds[transcript_source.source_identifier]
                    ),
                    overlap_count=len(overlap),
                    active_count=source_record["active"],
                    inactive_count=source_record["inactive"],
                    ambiguous_count=0,
                    metadata_completeness={"cell": 1.0, "dose": 1.0, "time": 1.0},
                    context=transcript_source.experimental_context,
                    limitations=["Offline fixture; no expression values were retrieved."],
                    computation_provenance=[raw_artifact],
                )
            )
    coverage_set = CombinationCoverageSet(
        workflow_id=workflow_id,
        discovery_round=0,
        combinations=coverage,
    )
    proposals = []
    for item in coverage[: fixture["expected_strategy_proposals"]]:
        proposals.append(
            StrategyProposal(
                proposal_id=f"proposal-{item.coverage_id}",
                included_source_combination=[
                    *item.activity_source_ids,
                    item.transcriptomic_source_id,
                ],
                modalities=[item.requested_modality],
                proposed_context=item.context,
                proposed_label_policy={"operator": "source_backed_activity_call"},
                expected_dataset_size=item.overlap_count,
                expected_class_balance={
                    "active": item.active_count or 0,
                    "inactive": item.inactive_count or 0,
                },
                identifier_losses=(item.activity_compound_count - item.overlap_count),
                metadata_losses={},
                scientific_strengths=["Source-backed activity and transcriptomic overlap."],
                scientific_risks=["Small offline fixture."],
                exclusions=["No cross-modality aggregation during discovery."],
                provenance_references=[raw_artifact],
                proposal_status=ProposalStatus.VIABLE,
            )
        )
    proposal_set = StrategyProposalSet(
        workflow_id=workflow_id,
        discovery_round=0,
        proposals=proposals,
    )
    return ledger, candidate_set, hydrated_set, coverage_set, proposal_set


def _single_modality_review_documents(service, workflow_id: str, modality: str):
    workflow = service.training_dataset_workflow(workflow_id)
    plan = workflow["discovery_plan"]
    activity_task = next(
        item
        for item in plan["provider_specific_query_tasks"]
        if item["evidence_role"] == "activity" and item.get("modality") == modality
    )
    transcriptomic_task = next(
        item
        for item in plan["provider_specific_query_tasks"]
        if item["evidence_role"] == "transcriptomic"
    )
    with service.database.session() as session:
        persisted = service.artifact_store._put_bytes(
            session,
            workflow_id=workflow_id,
            content=b'{"offline_fixture":true}',
            mime_type="application/json",
            artifact_type="offline_fixture_source_response",
            logical_name="non-receptor-offline-source-fixture.json",
            producer="offline-fixture-orchestrator",
            idempotency_key="non-receptor-offline-source-fixture",
        )
    raw = ArtifactReference(
        artifact_id=persisted.id,
        sha256=persisted.sha256,
        artifact_type=persisted.artifact_type,
    )
    records = []
    for item in plan["provider_specific_query_tasks"]:
        selected = item["task_id"] in {activity_task["task_id"], transcriptomic_task["task_id"]}
        records.append(
            DiscoveryExecutionRecord(
                task_id=item["task_id"],
                provider=item["provider"],
                evidence_role=item["evidence_role"],
                modality=item.get("modality"),
                status=(
                    DiscoveryTaskStatus.COMPLETED
                    if selected
                    else DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES
                ),
                pages_or_cursors_attempted=["fixture-page-1"],
                search_result_count=1 if selected else 0,
                unique_candidate_count=1 if selected else 0,
                source_response_artifacts=[raw],
                completion_reason="Offline fixture catalogue exhausted.",
            )
        )
    ledger = DiscoveryExecutionLedger(
        workflow_id=workflow_id,
        discovery_round=0,
        plan_fingerprint=plan["plan_fingerprint"],
        records=records,
    )
    candidate_activity = SourceCandidate(
        candidate_id="candidate-dna-activity",
        provider=activity_task["provider"],
        source_identifier="offline-dna-activity",
        evidence_role=EvidenceRole.ACTIVITY,
        modality=modality,
        discovery_task_id=activity_task["task_id"],
        discovery_query_provenance=[raw],
        provider_dataset_artifacts=[raw],
        candidate_status="metadata_candidate",
    )
    candidate_transcriptomic = SourceCandidate(
        candidate_id="candidate-dna-transcriptomic",
        provider=transcriptomic_task["provider"],
        source_identifier="offline-dna-transcriptomic",
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        discovery_task_id=transcriptomic_task["task_id"],
        discovery_query_provenance=[raw],
        provider_dataset_artifacts=[raw],
        candidate_status="metadata_candidate",
    )
    candidates = SourceCandidateSet(
        workflow_id=workflow_id,
        discovery_round=0,
        plan_fingerprint=plan["plan_fingerprint"],
        candidates=[candidate_activity, candidate_transcriptomic],
        provider_dataset_artifacts=[raw],
    )
    hydrated_activity = HydratedSource(
        hydrated_source_id="hydrated-dna-activity",
        source_candidate_id=candidate_activity.candidate_id,
        provider=candidate_activity.provider,
        source_identifier=candidate_activity.source_identifier,
        evidence_role=EvidenceRole.ACTIVITY,
        modality=modality,
        verified_metadata={"compound_ids": [f"CMPD-{index:04d}" for index in range(1, 37)]},
        compound_index_available=True,
        label_or_activity_fields_available=True,
        source_version="offline-v1",
        completeness_status=HydrationCompleteness.COMPLETE,
        evidence_artifacts=[raw],
    )
    hydrated_transcriptomic = HydratedSource(
        hydrated_source_id="hydrated-dna-transcriptomic",
        source_candidate_id=candidate_transcriptomic.candidate_id,
        provider=candidate_transcriptomic.provider,
        source_identifier=candidate_transcriptomic.source_identifier,
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        verified_metadata={"compound_ids": [f"CMPD-{index:04d}" for index in range(1, 37)]},
        compound_index_available=True,
        label_or_activity_fields_available=False,
        experimental_context={"cell": "CELL_A", "dose": "1 uM", "time": "24 h"},
        source_version="offline-v1",
        completeness_status=HydrationCompleteness.COMPLETE,
        evidence_artifacts=[raw],
    )
    hydrated = HydratedSourceSet(
        workflow_id=workflow_id,
        discovery_round=0,
        sources=[hydrated_activity, hydrated_transcriptomic],
        provider_dataset_artifacts=[raw],
    )
    coverage = CombinationCoverage(
        coverage_id="coverage-dna-activity-transcriptomic",
        activity_source_ids=[hydrated_activity.hydrated_source_id],
        transcriptomic_source_id=hydrated_transcriptomic.hydrated_source_id,
        requested_modality=modality,
        identity_resolution_count=36,
        activity_compound_count=36,
        transcriptomic_compound_count=36,
        overlap_count=36,
        active_count=18,
        inactive_count=18,
        ambiguous_count=0,
        expected_assembled_sample_count=72,
        expected_unique_compound_count=36,
        context=hydrated_transcriptomic.experimental_context,
        computation_provenance=[raw],
    )
    coverage_set = CombinationCoverageSet(
        workflow_id=workflow_id, discovery_round=0, combinations=[coverage]
    )
    proposal_set = StrategyProposalSet(
        workflow_id=workflow_id,
        discovery_round=0,
        proposals=[
            StrategyProposal(
                proposal_id="proposal-dna-pathway-activation",
                included_source_combination=[
                    hydrated_activity.hydrated_source_id,
                    hydrated_transcriptomic.hydrated_source_id,
                ],
                modalities=[modality],
                proposed_context=hydrated_transcriptomic.experimental_context,
                proposed_label_policy={"operator": "source_backed_activity_call"},
                expected_dataset_size=36,
                expected_unique_compounds=36,
                expected_class_balance={"active": 18, "inactive": 18},
                identifier_losses=0,
                scientific_strengths=["Endpoint-neutral offline fixture."],
                scientific_risks=["Synthetic data only."],
                exclusions=["Ambiguous labels"],
                provenance_references=[raw],
                proposal_status=ProposalStatus.VIABLE,
            )
        ],
    )
    return ledger, candidates, hydrated, coverage_set, proposal_set


def test_operational_registry_has_no_preapproval_provider_blockers() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    registry = load_operational_capability_registry(repo_root)
    lincs = registry.by_provider("lincs-l1000")
    assert lincs.declaration(
        OperationalCapability.PERTURBAGEN_CATALOGUE
    ).satisfies_preapproval_requirement
    assert lincs.declaration(
        OperationalCapability.COMPOUND_SIGNATURE_OVERLAP
    ).satisfies_preapproval_requirement
    assert not lincs.declaration(
        OperationalCapability.HEAVY_EXPRESSION_EXTRACTION
    ).satisfies_preapproval_requirement
    preapproval = [
        declaration
        for provider in registry.providers
        for declaration in provider.capabilities
        if declaration.phase is CapabilityPhase.PRE_APPROVAL
    ]
    assert preapproval
    assert all(item.availability is CapabilityAvailability.OPERATIONAL for item in preapproval)
    assert all(item.generic_fallback_prohibited for item in registry.providers)


def test_source_candidate_contract_has_no_selection_authority() -> None:
    with pytest.raises(ValidationError, match="final_selection"):
        SourceCandidate.model_validate(
            {
                "candidate_id": "candidate-x",
                "provider": "provider-x",
                "source_identifier": "source-x",
                "evidence_role": "activity",
                "modality": "binding",
                "discovery_task_id": "task-x",
                "candidate_status": "metadata_candidate",
                "final_selection": True,
            }
        )


def test_explicit_zero_compact_candidates_does_not_reuse_provider_row_count() -> None:
    evidence = ArtifactReference(
        artifact_id="artifact-provider-rows",
        sha256="a" * 64,
        artifact_type="normalized_provider_rows",
    )
    ledger = DiscoveryExecutionLedger(
        workflow_id="build-explicit-zero-compact",
        discovery_round=0,
        plan_fingerprint="b" * 64,
        records=[
            DiscoveryExecutionRecord(
                task_id="task-explicit-zero-compact",
                provider="provider-example",
                evidence_role=EvidenceRole.ACTIVITY,
                modality="binding",
                status=DiscoveryTaskStatus.COMPLETED,
                search_result_count=470,
                raw_record_count=470,
                normalized_record_count=470,
                provider_unique_record_count=470,
                compact_source_candidate_count=0,
                retained_row_count=470,
                unique_candidate_count=470,
                source_response_artifacts=[evidence],
                row_level_artifact_references=[evidence],
                deterministic_summary_artifacts=[evidence],
            )
        ],
    )
    candidates = SourceCandidateSet(
        workflow_id=ledger.workflow_id,
        discovery_round=ledger.discovery_round,
        plan_fingerprint=ledger.plan_fingerprint,
        candidates=[],
        all_planned_tasks_terminal=True,
        complete_without_failures=True,
        completed_task_ids=["task-explicit-zero-compact"],
        provider_dataset_artifacts=[evidence],
        raw_record_count=470,
        normalized_record_count=470,
        provider_unique_record_count=470,
        compact_source_candidate_count=0,
        retained_row_count=470,
    )

    validate_candidate_universe(ledger, candidates)


def test_row_level_accounting_compacts_470_records_without_evidence_loss(
    workflow_runtime, tmp_path
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_broad_build(service)
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="accounting-plan",
    )
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="accounting-authorize",
    )
    ledger, candidates, hydrated, coverage, proposals = _review_documents(service, approved.id)
    raw_reference = candidates.candidates[0].discovery_query_provenance[0]
    contributing_task = candidates.candidates[0].discovery_task_id
    records = []
    for item in ledger.records:
        if item.task_id == contributing_task:
            records.append(
                item.model_copy(
                    update={
                        "raw_record_count": 470,
                        "normalized_record_count": 470,
                        "provider_unique_record_count": 470,
                        "compact_source_candidate_count": item.unique_candidate_count,
                        "retained_row_count": 470,
                        "row_level_artifact_references": [raw_reference],
                        "deterministic_summary_artifacts": [raw_reference],
                    }
                )
            )
        else:
            records.append(item)
    ledger = ledger.model_copy(update={"records": records})
    candidates = candidates.model_copy(
        update={
            "raw_record_count": 470,
            "normalized_record_count": 470,
            "provider_unique_record_count": 470,
            "compact_source_candidate_count": len(candidates.candidates),
            "retained_row_count": 470,
            "provider_dataset_artifacts": [raw_reference],
        }
    )
    validate_candidate_universe(ledger, candidates)
    review = service.record_semantics_v2_discovery_review(
        approved.id,
        ledger=ledger,
        candidates=candidates,
        hydrated_sources=hydrated,
        combination_coverage=coverage,
        strategy_proposals=proposals,
        expected_version=authorized.version,
        actor="offline-fixture-orchestrator",
        idempotency_key="accounting-review",
    )
    assert review.current_stage is WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW
    persisted = service.training_dataset_workflow(approved.id)["source_candidates"]
    assert persisted["raw_record_count"] == 470
    assert persisted["provider_unique_record_count"] == 470
    assert persisted["compact_source_candidate_count"] == len(candidates.candidates)
    assert persisted["retained_row_count"] == 470
    assert len(persisted["candidates"]) < 470
    assert any(
        event["to_state"] == WorkflowState.HYDRATING_SOURCE_CANDIDATES.value
        for event in service.timeline(approved.id)
    )
    assert store.verify(raw_reference.artifact_id)


class _EmptyInput(ToolInput):
    pass


class _EmptyOutput(ToolOutput):
    ok: bool = True


def test_postapproval_tool_permission_is_enforced_by_registry() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="slice_approved_expression",
                description="Slice only recipe-approved expression signatures.",
                input_schema_name="EmptyInput",
                output_schema_name="EmptyOutput",
                required_permissions=[],
                side_effect=SideEffectClassification.ARTIFACT_WRITE,
                idempotency=IdempotencyClassification.IDEMPOTENT,
                timeout_seconds=30,
                allowed_workflow_stages=[
                    WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                    WorkflowState.ASSEMBLING_APPROVED_DATASET,
                ],
                implementation_version="test-v1",
                access_phase=ToolAccessPhase.POST_APPROVAL_EXTRACTION,
            ),
            input_model=_EmptyInput,
            output_model=_EmptyOutput,
            implementation=lambda _input: _EmptyOutput(),
        )
    )
    prohibited = registry.invoke(
        ToolInvocation(
            tool_name="slice_approved_expression",
            arguments={},
            workflow_stage=WorkflowState.HYDRATING_SOURCE_CANDIDATES,
        )
    )
    assert prohibited.status is ToolCallStatus.PROHIBITED
    allowed = registry.invoke(
        ToolInvocation(
            tool_name="slice_approved_expression",
            arguments={},
            workflow_stage=WorkflowState.ASSEMBLING_APPROVED_DATASET,
        )
    )
    assert allowed.status is ToolCallStatus.COMPLETED


def test_semantics_v2_end_to_end_preserves_universe_and_approves_one_recipe(
    workflow_runtime, tmp_path
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_broad_build(service)
    with pytest.raises(InvalidTransition, match="cannot enter legacy"):
        service.transition(
            approved.id,
            TransitionRequest(
                target_state=WorkflowState.DERIVING_COMPONENT_REQUIREMENTS,
                expected_version=approved.version,
                idempotency_key="semantics-v2:legacy-state-forbidden",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="test-orchestrator",
            ),
        )
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="semantics-v2-plan",
    )
    assert planned.current_stage is WorkflowState.DISCOVERY_PLANNING
    workflow = service.training_dataset_workflow(approved.id)
    assert workflow["workflow_semantics_version"] == WORKFLOW_SEMANTICS_V2
    assert len(workflow["discovery_plan"]["provider_specific_query_tasks"]) == 10
    assert len(workflow["discovery_execution_ledger"]["records"]) == 10
    findings = workflow["provider_capability_findings"]["findings"]
    assert {
        (item["provider"], item["missing_capability"], item["finding_type"]) for item in findings
    } == {
        (
            "activity-beta",
            "related_assay_relationships",
            "REGISTERED_PROVIDER_CAPABILITY_MISSING",
        )
    }
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="semantics-v2-authorize",
    )
    assert authorized.current_stage is WorkflowState.DISCOVERING_SOURCE_CANDIDATES
    documents = _review_documents(service, approved.id)
    with pytest.raises(ValueError, match="complete unique-candidate counts"):
        validate_candidate_universe(
            documents[0],
            documents[1].model_copy(update={"candidates": documents[1].candidates[:-1]}),
        )
    review = service.record_semantics_v2_discovery_review(
        approved.id,
        ledger=documents[0],
        candidates=documents[1],
        hydrated_sources=documents[2],
        combination_coverage=documents[3],
        strategy_proposals=documents[4],
        expected_version=authorized.version,
        actor="offline-fixture-orchestrator",
        idempotency_key="semantics-v2-review",
    )
    assert review.current_stage is WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW
    workflow = service.training_dataset_workflow(approved.id)
    assert len(workflow["source_candidates"]["candidates"]) == 7
    assert len(workflow["hydrated_sources"]["sources"]) == 7
    assert len(workflow["combination_coverage"]["combinations"]) == 10
    assert len(workflow["strategy_proposals"]["proposals"]) == 3
    assert workflow["assembly_recipe"] is None
    assert not any(
        item.artifact_type in {"assembled_dataset", "expression_matrix", "training_table"}
        for item in store.list_artifacts(approved.id)
    )
    proposal = workflow["strategy_proposals"]["proposals"][0]
    recipe_approved = service.approve_semantics_v2_strategy(
        approved.id,
        strategy_proposal_id=proposal["proposal_id"],
        approved_modality_aggregation={"operator": "none", "preserve_original": True},
        approved_context_filters=proposal["proposed_context"],
        approved_dose_time_rules={"rule": "exact fixture context"},
        approved_label_policy=proposal["proposed_label_policy"],
        exclusion_rules=proposal["exclusions"],
        required_extraction_fields=[
            "canonical_compound_id",
            "transcriptomic_signature",
            "endpoint_activity_label",
            "provenance",
        ],
        expected_version=review.version,
        actor="test-reviewer",
        idempotency_key="semantics-v2-recipe-approval",
    )
    assert recipe_approved.current_stage is WorkflowState.ASSEMBLY_RECIPE_APPROVED
    with pytest.raises(InvalidTransition, match="requires human"):
        service.transition(
            approved.id,
            TransitionRequest(
                target_state=WorkflowState.ASSEMBLING_APPROVED_DATASET,
                expected_version=recipe_approved.version,
                idempotency_key="semantics-v2:assembly-deferred",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="test-orchestrator",
            ),
        )
    recipe = service.training_dataset_workflow(approved.id)["assembly_recipe"]
    assert recipe["selected_strategy_proposal_id"] == proposal["proposal_id"]
    assert recipe["immutable_status"] == "approved_immutable"
    artifacts_before = [(item.id, item.sha256) for item in store.list_artifacts(approved.id)]
    events_before = len(service.timeline(approved.id))
    assert service.get_build(approved.id).current_stage is WorkflowState.ASSEMBLY_RECIPE_APPROVED
    assert service.training_dataset_workflow(approved.id)["assembly_recipe"] == recipe
    assert [
        (item.id, item.sha256) for item in store.list_artifacts(approved.id)
    ] == artifacts_before
    assert len(service.timeline(approved.id)) == events_before


def test_zero_candidate_coverage_uses_the_configured_artifact_store(
    workflow_runtime, tmp_path
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_broad_build(service)
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="zero-candidate-coverage:plan",
    )
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="zero-candidate-coverage:authorize",
    )
    hydrated = HydratedSourceSet(
        workflow_id=approved.id,
        discovery_round=0,
        sources=[],
    )
    with database.session() as session:
        workflow = session.get(TrainingDatasetWorkflowRow, approved.id)
        build = session.get(EndpointBuildRow, approved.id)
        assert workflow is not None
        assert build is not None
        workflow.hydrated_sources_json = hydrated.model_dump_json()
        build.current_stage = WorkflowState.COMPUTING_COMBINATION_COVERAGE.value

    result = service.calculate_semantics_v2_coverage(
        approved.id,
        expected_version=authorized.version,
        actor="deterministic-orchestrator",
        idempotency_key="zero-candidate-coverage:calculate",
    )

    assert result.current_stage is WorkflowState.GENERATING_ASSEMBLY_STRATEGIES
    workflow = service.training_dataset_workflow(approved.id)
    assert workflow["combination_coverage"]["combinations"] == []
    coverage_artifact = store.find_by_logical_name(approved.id, "combination-coverage-round-0.json")
    assert coverage_artifact is not None
    assert store.verify(coverage_artifact.id)


def test_revision_and_rejection_create_new_round_without_overwriting_prior_artifacts(
    workflow_runtime, tmp_path
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_broad_build(service)
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="semantics-v2-plan",
    )
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="semantics-v2-authorize",
    )
    documents = _review_documents(service, approved.id)
    review = service.record_semantics_v2_discovery_review(
        approved.id,
        ledger=documents[0],
        candidates=documents[1],
        hydrated_sources=documents[2],
        combination_coverage=documents[3],
        strategy_proposals=documents[4],
        expected_version=authorized.version,
        actor="offline-fixture-orchestrator",
        idempotency_key="semantics-v2-review",
    )
    rejected = service.reject_semantics_v2_strategy_set(
        approved.id,
        reason=(
            "The proposal proves identity joinability but is insufficient for "
            "compound-grouped benchmarking."
        ),
        expected_version=review.version,
        actor="test-reviewer",
        idempotency_key="semantics-v2-reject",
        reason_category="insufficient_compound_count_and_class_balance",
        revision_objective="Expand source-declared active and inactive activity evidence.",
        technical_proof_classification="TECHNICAL_PROOF_OF_JOINABILITY",
        technical_proof_proposal_ids=[documents[4].proposals[0].proposal_id],
    )
    assert rejected.current_stage is WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW
    assert rejected.version == review.version
    workflow_after_rejection = service.training_dataset_workflow(approved.id)
    rejection = workflow_after_rejection["strategy_set_rejection"]
    assert rejection["decision"] == "rejected"
    assert rejection["reviewed_workflow_version"] == review.version
    assert rejection["reason_category"] == "insufficient_compound_count_and_class_balance"
    assert rejection["technical_proof_classification"] == "TECHNICAL_PROOF_OF_JOINABILITY"
    assert rejection["technical_proof_proposal_ids"] == [
        documents[4].proposals[0].proposal_id
    ]
    assert {item["artifact_type"] for item in rejection["preserved_artifacts"]} == {
        "source_candidates",
        "hydrated_sources",
        "combination_coverage",
        "strategy_proposals",
    }
    assert rejection["proposal_strengths"][documents[4].proposals[0].proposal_id]
    assert rejection["proposal_limitations"][documents[4].proposals[0].proposal_id]
    with pytest.raises(GuardNotSatisfied, match="current strategy set was rejected"):
        service.approve_semantics_v2_strategy(
            approved.id,
            strategy_proposal_id=documents[4].proposals[0].proposal_id,
            approved_modality_aggregation={"operator": "none"},
            approved_context_filters={},
            approved_dose_time_rules={},
            approved_label_policy={"operator": "source_backed_activity_call"},
            exclusion_rules=documents[4].proposals[0].exclusions,
            required_extraction_fields=["canonical_compound_identifier"],
            expected_version=rejected.version,
            actor="test-reviewer",
            idempotency_key="rejected-proposal-cannot-be-approved",
        )
    rejection_artifacts = [(item.id, item.sha256) for item in store.list_artifacts(approved.id)]
    repeated_rejection = service.reject_semantics_v2_strategy_set(
        approved.id,
        reason=(
            "The proposal proves identity joinability but is insufficient for "
            "compound-grouped benchmarking."
        ),
        expected_version=review.version,
        actor="test-admin",
        idempotency_key="semantics-v2-reject",
        reason_category="insufficient_compound_count_and_class_balance",
        revision_objective="Expand source-declared active and inactive activity evidence.",
        technical_proof_classification="TECHNICAL_PROOF_OF_JOINABILITY",
        technical_proof_proposal_ids=[documents[4].proposals[0].proposal_id],
    )
    assert repeated_rejection.version == rejected.version
    assert [
        (item.id, item.sha256) for item in store.list_artifacts(approved.id)
    ] == rejection_artifacts
    old_artifacts = {(item.id, item.sha256) for item in store.list_artifacts(approved.id)}
    revised = service.request_semantics_v2_discovery_revision(
        approved.id,
        reason="Restrict the next round to one transcriptomic context.",
        provider_policy_revision={},
        modality_policy_revision={},
        context_constraint_revision={"cell": "CELL_A"},
        expected_version=rejected.version,
        actor="test-reviewer",
        idempotency_key="semantics-v2-revision",
    )
    assert revised.current_stage is WorkflowState.DISCOVERY_PLANNING
    repeated_revision = service.request_semantics_v2_discovery_revision(
        approved.id,
        reason="Restrict the next round to one transcriptomic context.",
        provider_policy_revision={},
        modality_policy_revision={},
        context_constraint_revision={"cell": "CELL_A"},
        expected_version=rejected.version,
        actor="test-reviewer",
        idempotency_key="semantics-v2-revision",
    )
    assert repeated_revision.version == revised.version
    workflow = service.training_dataset_workflow(approved.id)
    assert workflow["discovery_round"] == 1
    assert workflow["source_candidates"] is None
    assert workflow["assembly_recipe"] is None
    new_artifacts = {(item.id, item.sha256) for item in store.list_artifacts(approved.id)}
    assert old_artifacts < new_artifacts
    assert (
        len(
            [
                item
                for item in store.list_artifacts(approved.id)
                if item.artifact_type == "discovery_plan"
            ]
        )
        == 2
    )


def test_historical_semantics_v1_build_remains_read_only_on_refresh(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Historical receptor endpoint",
            endpoint_slug="historical-receptor-endpoint",
            biological_goal="Preserve a historical endpoint build without automatic migration.",
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            idempotency_key="historical-semantics-v1",
        )
    )
    with database.session() as session:
        row = session.get(TrainingDatasetWorkflowRow, build.id)
        assert row is not None
        row.workflow_semantics_version = "1.0.0"
    artifacts_before = [(item.id, item.sha256) for item in store.list_artifacts(build.id)]
    first = service.training_dataset_workflow(build.id)
    second = service.training_dataset_workflow(build.id)
    assert first == second
    assert first["legacy"] is True
    assert first["legacy_semantics_read_only"] is True
    assert first["workflow_semantics_version"] == "1.0.0"
    assert [(item.id, item.sha256) for item in store.list_artifacts(build.id)] == artifacts_before
    with pytest.raises(WorkflowConflict, match="read-only"):
        service.start_build(
            build.id,
            expected_version=build.version,
            actor="test-admin",
            idempotency_key="historical-semantics-v1:start-forbidden",
        )
    assert [(item.id, item.sha256) for item in store.list_artifacts(build.id)] == artifacts_before
    with database.session() as session:
        persisted = session.scalar(
            select(TrainingDatasetWorkflowRow).where(
                TrainingDatasetWorkflowRow.workflow_id == build.id
            )
        )
        assert persisted is not None
        assert persisted.workflow_semantics_version == "1.0.0"


def test_second_fixture_is_endpoint_neutral_and_forbids_modality_union() -> None:
    fixture = json.loads((FIXTURES / "semantics_v2_non_receptor_toxicity.json").read_text())
    assert fixture["target"] == "Mitochondrial membrane potential loss"
    assert fixture["modalities"] == ["depolarization"]
    assert fixture["modality_union_allowed"] is False
    assert fixture["expression_values_retrieved"] is False


def test_complete_offline_endpoint_lifecycle_reaches_registry_publication(
    workflow_runtime, tmp_path
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_broad_build(service)
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="stage2-plan",
    )
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="stage2-authorize",
    )
    documents = _review_documents(service, approved.id)
    review = service.record_semantics_v2_discovery_review(
        approved.id,
        ledger=documents[0],
        candidates=documents[1],
        hydrated_sources=documents[2],
        combination_coverage=documents[3],
        strategy_proposals=documents[4],
        expected_version=authorized.version,
        actor="offline-fixture-orchestrator",
        idempotency_key="stage2-review",
    )
    proposal = service.training_dataset_workflow(approved.id)["strategy_proposals"]["proposals"][0]
    recipe = service.approve_semantics_v2_strategy(
        approved.id,
        strategy_proposal_id=proposal["proposal_id"],
        approved_modality_aggregation={"operator": "none", "preserve_original": True},
        approved_context_filters=proposal["proposed_context"],
        approved_dose_time_rules={"rule": "exact approved context"},
        approved_label_policy={"operator": "source_backed_activity_call"},
        exclusion_rules=proposal["exclusions"],
        required_extraction_fields=[
            "compound_id",
            "signature_id",
            "source_activity_record",
            "provenance",
        ],
        expected_version=review.version,
        actor="test-reviewer",
        idempotency_key="stage2-recipe",
    )
    assembled = service.run_approved_dataset_assembly(
        approved.id,
        offline_input=build_offline_assembly_input(
            "tr_receptor",
            activity_source_id=proposal["included_source_combination"][0],
            transcriptomic_source_id=proposal["included_source_combination"][1],
        ),
        expected_version=recipe.version,
        actor="test-reviewer",
        idempotency_key="stage2-assembly",
    )
    assert assembled.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL
    workflow = service.training_dataset_workflow(approved.id)
    assert workflow["dataset_bundle"]["large_data_inline"] is False
    assert workflow["dataset_quality_report"]["leakage_check_passed"] is True
    with pytest.raises(InvalidTransition):
        service.run_endpoint_benchmark(
            approved.id,
            expected_version=assembled.version,
            actor="test-reviewer",
            idempotency_key="stage2-benchmark-before-dataset-approval",
        )
    approved_dataset = service.approve_dataset_for_benchmarking(
        approved.id,
        rationale="Quality and leakage review passed.",
        expected_version=assembled.version,
        actor="test-reviewer",
        idempotency_key="stage2-dataset-approval",
    )
    assert approved_dataset.current_stage is WorkflowState.AWAITING_TRAINING_APPROVAL
    benchmarked = service.run_endpoint_benchmark(
        approved.id,
        expected_version=approved_dataset.version,
        actor="test-reviewer",
        idempotency_key="stage2-benchmark",
    )
    assert benchmarked.current_stage is WorkflowState.AWAITING_SCIENTIFIC_APPROVAL
    results = service.training_dataset_workflow(approved.id)["model_benchmark_results"]
    assert {item["model_name"] for item in results} == {
        "logistic_regression",
        "random_forest",
        "hist_gradient_boosting",
    }
    with pytest.raises(GuardNotSatisfied):
        service.publish_validated_endpoint(
            approved.id,
            expected_version=benchmarked.version,
            actor="test-reviewer",
            idempotency_key="stage2-publication-before-model-selection",
        )
    selected = service.select_and_validate_endpoint_model(
        approved.id,
        candidate_id="candidate-logistic_regression",
        decision_threshold=0.5,
        rationale="Selected after multi-metric review.",
        expected_version=benchmarked.version,
        actor="test-reviewer",
        idempotency_key="stage2-selection",
    )
    assert selected.current_stage is WorkflowState.AWAITING_SCIENTIFIC_APPROVAL
    published = service.publish_validated_endpoint(
        approved.id,
        expected_version=selected.version,
        actor="test-reviewer",
        idempotency_key="stage2-publication",
    )
    assert published.current_stage is WorkflowState.COMPLETED
    final = service.training_dataset_workflow(approved.id)
    assert final["publication_receipt"]["model_library_visible"] is True
    assert final["implementation_validation_status"]["live_validation_status"] == (
        "live_validation_pending"
    )
    assert final["implementation_validation_status"]["scientific_validation_status"] == (
        "scientifically_validation_pending"
    )
    assert (tmp_path / "registry" / "models" / "endpoints.json").is_file()
    explore_dir = tmp_path / "models" / "EXAMPLE_RECEPTOR_BROAD_V2" / "explore"
    explore_map = json.loads((explore_dir / "umap.json").read_text(encoding="utf-8"))
    explore_manifest = json.loads((explore_dir / "manifest.json").read_text(encoding="utf-8"))
    support_content = (explore_dir / "support.npy").read_bytes()
    assert len(explore_map["points"]) == explore_manifest["n_compounds"]
    assert hashlib.sha256(support_content).hexdigest() == explore_manifest["source"]["sha256"]
    assert hashlib.sha256(support_content).hexdigest() == explore_manifest["support_sha256"]
    assert explore_manifest["recomputed_on_page_load"] is False
    assert not any(
        item.artifact_type == "scientific_source_request"
        for item in store.list_artifacts(approved.id)
    )


def test_non_receptor_offline_lifecycle_is_endpoint_neutral(workflow_runtime, tmp_path) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    _write_fixture_registry(service, tmp_path)
    approved = _start_approved_fixture_build(service, "dna_damage")
    assert approved.current_stage is WorkflowState.APPROVED_SPECIFICATION
    planned = service.continue_training_dataset_workflow(
        approved.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="stage2-dna-plan",
    )
    authorized = service.authorize_semantics_v2_discovery(
        approved.id,
        expected_version=planned.version,
        actor="test-admin",
        idempotency_key="stage2-dna-authorize",
    )
    documents = _single_modality_review_documents(service, approved.id, "pathway_activation")
    review = service.record_semantics_v2_discovery_review(
        approved.id,
        ledger=documents[0],
        candidates=documents[1],
        hydrated_sources=documents[2],
        combination_coverage=documents[3],
        strategy_proposals=documents[4],
        expected_version=authorized.version,
        actor="offline-fixture-orchestrator",
        idempotency_key="stage2-dna-review",
    )
    proposal = documents[4].proposals[0]
    recipe = service.approve_semantics_v2_strategy(
        approved.id,
        strategy_proposal_id=proposal.proposal_id,
        approved_modality_aggregation={"operator": "none", "preserve_original": True},
        approved_context_filters=proposal.proposed_context,
        approved_dose_time_rules={"rule": "exact approved context"},
        approved_label_policy=proposal.proposed_label_policy,
        exclusion_rules=proposal.exclusions,
        required_extraction_fields=["compound_id", "signature_id", "provenance"],
        expected_version=review.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-recipe",
    )
    assembled = service.run_approved_dataset_assembly(
        approved.id,
        offline_input=build_offline_assembly_input(
            "dna_damage",
            activity_source_id=proposal.included_source_combination[0],
            transcriptomic_source_id=proposal.included_source_combination[1],
        ),
        expected_version=recipe.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-assembly",
    )
    approved_dataset = service.approve_dataset_for_benchmarking(
        approved.id,
        rationale="Quality and leakage review passed.",
        expected_version=assembled.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-dataset-approval",
    )
    benchmarked = service.run_endpoint_benchmark(
        approved.id,
        expected_version=approved_dataset.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-benchmark",
    )
    selected = service.select_and_validate_endpoint_model(
        approved.id,
        candidate_id="candidate-random_forest",
        decision_threshold=0.5,
        rationale="Selected after multi-metric review.",
        expected_version=benchmarked.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-selection",
    )
    published = service.publish_validated_endpoint(
        approved.id,
        expected_version=selected.version,
        actor="test-reviewer",
        idempotency_key="stage2-dna-publication",
    )
    assert published.current_stage is WorkflowState.COMPLETED
    workflow = service.training_dataset_workflow(approved.id)
    assert workflow["assembly_recipe"]["approved_modalities"] == ["pathway_activation"]
    assert workflow["publication_receipt"]["endpoint_id"] == "OFFLINE_DNA_DAMAGE"
    assert workflow["publication_receipt"]["model_library_visible"] is True


def test_coverage_engine_evaluates_all_compact_source_combinations() -> None:
    reference = ArtifactReference(
        artifact_id="artifact-coverage",
        sha256="a" * 64,
        artifact_type="provider_rows",
    )
    sources = HydratedSourceSet(
        workflow_id="workflow-coverage",
        discovery_round=2,
        sources=[
            HydratedSource(
                hydrated_source_id="activity-binding",
                source_candidate_id="candidate-binding",
                provider="activity-provider",
                source_identifier="assay-binding",
                evidence_role=EvidenceRole.ACTIVITY,
                modality="binding",
                verified_metadata={"compound_ids": ["C1", "C2", "C3"]},
                compound_index_available=True,
                label_or_activity_fields_available=True,
                source_version="v1",
                completeness_status=HydrationCompleteness.COMPLETE,
                coverage_summary={"row_count": 470, "active_count": 2, "inactive_count": 1},
                evidence_artifacts=[reference],
            ),
            HydratedSource(
                hydrated_source_id="activity-agonism",
                source_candidate_id="candidate-agonism",
                provider="activity-provider",
                source_identifier="assay-agonism",
                evidence_role=EvidenceRole.ACTIVITY,
                modality="agonism",
                verified_metadata={"compound_ids": ["C2", "C4"]},
                compound_index_available=True,
                label_or_activity_fields_available=True,
                source_version="v1",
                completeness_status=HydrationCompleteness.COMPLETE,
                evidence_artifacts=[reference],
            ),
            HydratedSource(
                hydrated_source_id="transcriptomic-one",
                source_candidate_id="candidate-transcriptomic",
                provider="transcriptomic-provider",
                source_identifier="catalogue-one",
                evidence_role=EvidenceRole.TRANSCRIPTOMIC,
                verified_metadata={"compound_ids": ["C2", "C3", "C5"]},
                compound_index_available=True,
                label_or_activity_fields_available=False,
                experimental_context={"cell": ["CELL_A"], "dose": ["1 uM"], "time": ["24 h"]},
                source_version="v1",
                completeness_status=HydrationCompleteness.COMPLETE,
                coverage_summary={"profile_count": 5},
                evidence_artifacts=[reference],
            ),
        ],
    )
    coverage = calculate_combination_coverage("workflow-coverage", 2, sources)
    assert len(coverage.combinations) == 2
    assert {item.requested_modality: item.overlap_count for item in coverage.combinations} == {
        "binding": 2,
        "agonism": 1,
    }
    binding = next(item for item in coverage.combinations if item.requested_modality == "binding")
    assert binding.activity_row_count == 470
    assert binding.computation_provenance == [reference]
    assert binding.context_completeness == 1.0


class _FixtureArtifactStore:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths

    def verified_path(self, artifact_id: str) -> tuple[object, Path]:
        return object(), self.paths[artifact_id]


def test_tr_joinability_uses_stable_bridges_and_blocks_unusable_transcriptomics(
    tmp_path: Path,
) -> None:
    activity_path = tmp_path / "activity.sqlite"
    with sqlite3.connect(activity_path) as connection:
        connection.executescript(
            """
            CREATE TABLE activity_records (
                assay_identifier TEXT, compound_identifier TEXT, cid TEXT,
                activity_call TEXT
            );
            CREATE TABLE compound_index (
                compound_identifier TEXT, cid TEXT, sid TEXT, inchikey TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO compound_index VALUES (?,?,?,?)",
            [
                ("DTXSID-A", "101", None, None),
                ("DTXSID-B", "102", None, None),
                ("DTXSID-C", "103", None, None),
                ("DTXSID-D", "104", None, None),
            ],
        )
        connection.executemany(
            "INSERT INTO activity_records VALUES (?,?,?,?)",
            [
                ("BIND", "DTXSID-A", None, "active"),
                ("BIND", "DTXSID-B", None, "inactive"),
                ("AGON", "DTXSID-B", None, "active"),
                ("AGON", "DTXSID-C", None, "inactive"),
                ("ANTAG", "DTXSID-C", None, "active"),
                ("ANTAG", "DTXSID-D", None, "inactive"),
            ],
        )

    perturbagen_path = tmp_path / "pert-info.sqlite"
    with sqlite3.connect(perturbagen_path) as connection:
        connection.execute(
            """
            CREATE TABLE records (
                pert_id TEXT, pubchem_cid TEXT, inchikey TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO records VALUES (?,?,?)",
            [
                ("BRD-A", "CID:101", None),
                ("BRD-B", "CID:102", None),
                ("BRD-C", "CID:103", None),
                ("BRD-X", "CID:999", None),
            ],
        )

    signature_path = tmp_path / "sig-info.sqlite"
    with sqlite3.connect(signature_path) as connection:
        connection.execute(
            """
            CREATE TABLE records (
                pert_id TEXT, cell_id TEXT, dose TEXT, exposure_time TEXT,
                measured_chemical_perturbation INTEGER, row_status TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO records VALUES (?,?,?,?,?,?)",
            [
                ("BRD-A", "MCF7", "1 uM", "24 h", 1, "valid"),
                ("BRD-B", "MCF7", "1 uM", "24 h", 1, "valid"),
                ("BRD-C", "A549", "10 uM", "6 h", 1, "valid"),
                ("BRD-X", "A549", "10 uM", "6 h", 1, "valid"),
            ],
        )

    geo_path = tmp_path / "geo.sqlite"
    with sqlite3.connect(geo_path) as connection:
        connection.execute(
            """
            CREATE TABLE transcriptomic_records (
                accession TEXT, source_identifier TEXT, compound_identifier TEXT,
                biological_model TEXT, dose TEXT, exposure_time TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO transcriptomic_records VALUES (?,?,?,?,?,?)",
            ("GSE12345", "GSE12345", None, "human hepatocytes", "1 uM", "24 h"),
        )

    references = {
        name: ArtifactReference(
            artifact_id=name,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            artifact_type="provider_rows",
        )
        for name in ("activity-db", "pert-info", "sig-info", "geo-db")
    }
    activity_sources = [
        HydratedSource(
            hydrated_source_id=f"activity-{modality}",
            source_candidate_id=f"candidate-{modality}",
            provider="pubchem-bioassay",
            source_identifier=f"AID:{assay}",
            evidence_role=EvidenceRole.ACTIVITY,
            modality=modality,
            verified_metadata={
                "activity_data_availability_inspected": True,
                "assay_relationships_inspected": True,
                "structural_validation_status": "valid",
            },
            compound_index_available=True,
            label_or_activity_fields_available=True,
            completeness_status=HydrationCompleteness.COMPLETE,
            evidence_artifacts=[references["activity-db"]],
        )
        for modality, assay in (
            ("binding", "BIND"),
            ("agonism", "AGON"),
            ("antagonism", "ANTAG"),
        )
    ]
    lincs_metadata = {
        "transcriptomic_source_class": "primary_lincs_l1000",
        "joinability_eligible": True,
        "stable_identity_bridge_available": True,
        "expression_extraction_path": "registered_post_approval_level5_gctx",
    }
    transcriptomic_sources = [
        HydratedSource(
            hydrated_source_id="transcriptomic-lincs",
            source_candidate_id="candidate-lincs",
            provider="lincs-l1000",
            source_identifier="LINCS-2020",
            evidence_role=EvidenceRole.TRANSCRIPTOMIC,
            verified_metadata=lincs_metadata,
            compound_index_available=True,
            label_or_activity_fields_available=False,
            completeness_status=HydrationCompleteness.COMPLETE,
            evidence_artifacts=[references["pert-info"], references["sig-info"]],
        ),
        HydratedSource(
            hydrated_source_id="transcriptomic-no-expression",
            source_candidate_id="candidate-no-expression",
            provider="registered-transcriptomics",
            source_identifier="NO-EXPRESSION",
            evidence_role=EvidenceRole.TRANSCRIPTOMIC,
            verified_metadata={
                **lincs_metadata,
                "expression_extraction_path": "unavailable",
            },
            compound_index_available=True,
            label_or_activity_fields_available=False,
            completeness_status=HydrationCompleteness.PARTIAL,
            evidence_artifacts=[references["pert-info"], references["sig-info"]],
        ),
        HydratedSource(
            hydrated_source_id="transcriptomic-geo",
            source_candidate_id="candidate-geo",
            provider="ncbi-geo",
            source_identifier="GSE12345",
            evidence_role=EvidenceRole.TRANSCRIPTOMIC,
            verified_metadata={
                "transcriptomic_source_class": "geo_supplemental",
                "joinability_eligible": False,
                "stable_identity_bridge_available": False,
                "expression_extraction_path": "approved_processed_matrix_locator",
            },
            compound_index_available=False,
            label_or_activity_fields_available=False,
            experimental_context={
                "cell": ["human hepatocytes"],
                "dose": ["1 uM"],
                "time": ["24 h"],
            },
            completeness_status=HydrationCompleteness.SCIENTIFICALLY_UNUSABLE,
            explicit_missing_fields=["stable_compound_identifier"],
            exclusion_reason="Supplemental GEO study lacks a stable compound bridge.",
            evidence_artifacts=[references["geo-db"]],
        ),
    ]
    sources = HydratedSourceSet(
        workflow_id="workflow-tr-joinability",
        discovery_round=1,
        sources=[*activity_sources, *transcriptomic_sources],
    )
    store = _FixtureArtifactStore(
        {
            "activity-db": activity_path,
            "pert-info": perturbagen_path,
            "sig-info": signature_path,
            "geo-db": geo_path,
        }
    )

    coverage = calculate_combination_coverage(
        "workflow-tr-joinability",
        1,
        sources,
        artifact_store=store,  # type: ignore[arg-type]
    )

    lincs = [
        item
        for item in coverage.combinations
        if item.transcriptomic_source_id == "transcriptomic-lincs"
    ]
    assert {item.requested_modality: item.overlap_count for item in lincs} == {
        "binding": 2,
        "agonism": 2,
        "antagonism": 1,
    }
    assert all(item.expected_assembled_sample_count > 0 for item in lincs)
    assert all(item.context_completeness == 1.0 for item in lincs)
    assert all(not item.quality_flags for item in lincs)
    assert {item.requested_modality: item.class_balance for item in lincs} == {
        "binding": {"active": 1.0, "inactive": 1.0, "ambiguous": 0.0},
        "agonism": {"active": 1.0, "inactive": 1.0, "ambiguous": 0.0},
        "antagonism": {"active": 1.0, "inactive": 0.0, "ambiguous": 0.0},
    }
    geo = [
        item
        for item in coverage.combinations
        if item.transcriptomic_source_id == "transcriptomic-geo"
    ]
    assert all(item.overlap_count == 0 for item in geo)
    assert all(
        "transcriptomic_source_not_joinability_eligible" in item.quality_flags for item in geo
    )
    missing_expression = [
        item
        for item in coverage.combinations
        if item.transcriptomic_source_id == "transcriptomic-no-expression"
    ]
    assert all(item.overlap_count > 0 for item in missing_expression)
    assert all(
        "expression_extraction_path_unavailable" in item.quality_flags
        for item in missing_expression
    )

    proposals = DeterministicAssemblyStrategyAgent().generate(
        StrategyAgentInput(
            workflow_id="workflow-tr-joinability",
            approved_endpoint_specification={"target": "thyroid hormone receptor"},
            hydrated_sources=sources,
            coverage=coverage,
            allowed_scientific_policies=["source_backed_activity_call"],
            quality_constraints=["stable compound identifiers required"],
        )
    )
    assert {
        proposal.modalities[0]
        for proposal in proposals.proposals
        if proposal.proposal_status is ProposalStatus.VIABLE
    } == {"binding", "agonism", "antagonism"}
    assert all(len(proposal.modalities) == 1 for proposal in proposals.proposals)


def test_offline_demo_replay_is_idempotent_and_artifact_stable(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    output_root = tmp_path / "offline-replay"
    first = execute_offline_demo(repo_root, output_root, "tr_receptor")
    second = execute_offline_demo(repo_root, output_root, "tr_receptor")
    assert first == second
    assert first["state"] == "COMPLETED"
    assert first["endpoint_id"] == "OFFLINE_TR_ACTIVITY"
    assert first["network_requests"] == 0
    assert first["openai_requests"] == 0
    assert first["gctx_network_access"] is False
