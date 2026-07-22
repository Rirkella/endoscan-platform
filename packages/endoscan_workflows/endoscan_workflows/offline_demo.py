"""Deterministic, network-free fixtures for the complete endpoint lifecycle."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

from .artifacts import LocalArtifactStore
from .contracts import (
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    WorkflowKind,
)
from .database import WorkflowDatabase
from .discovery_strategy import (
    ArtifactReference,
    CandidateStatus,
    CombinationCoverage,
    CombinationCoverageSet,
    DiscoveryExecutionLedger,
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    EvidenceRole,
    HydratedSource,
    HydratedSourceSet,
    HydrationCompleteness,
    ProposalStatus,
    SourceCandidate,
    SourceCandidateSet,
    StrategyProposal,
    StrategyProposalSet,
)
from .endpoint_lifecycle import OfflineAssemblyInput
from .service import WorkflowService
from .state_machine import WorkflowGraph, canonical_workflow_graph_path
from .training_dataset import (
    EndpointDiscoveryMode,
    EndpointDiscoveryScope,
    EndpointDiscoveryScopeProvenance,
    EndpointSemanticModality,
)

FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures")
FIXTURE_NAMES = {
    "tr_receptor": "emergency_stage2_tr_receptor.json",
    "dna_damage": "emergency_stage2_dna_damage.json",
}


def load_offline_demo_definition(name: str) -> dict[str, Any]:
    try:
        filename = FIXTURE_NAMES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown offline endpoint demo {name!r}.") from exc
    return json.loads((FIXTURE_DIRECTORY / filename).read_text(encoding="utf-8"))


def build_offline_assembly_input(
    name: str,
    *,
    activity_source_id: str | None = None,
    transcriptomic_source_id: str | None = None,
) -> OfflineAssemblyInput:
    """Generate small source-like rows without provider, OpenAI or GCTX access."""

    definition = load_offline_demo_definition(name)
    modalities = definition["modalities"]
    approved = definition["approved_modality"]
    activity_source_id = activity_source_id or f"hydrated-offline-{approved}-activity"
    transcriptomic_source_id = (
        transcriptomic_source_id or f"hydrated-offline-{approved}-transcriptomic"
    )
    compound_count = 36 if len(modalities) == 1 else 72
    activity_rows = []
    transcriptomic_rows = []
    expression_rows = []
    for index in range(compound_count):
        compound_id = f"CMPD-{index + 1:04d}"
        modality = modalities[index % len(modalities)]
        label = (index // len(modalities)) % 2
        activity_rows.append(
            {
                "compound_id": compound_id,
                "source_id": activity_source_id,
                "assay_id": f"ASSAY-{modality.upper()}-{index + 1:04d}",
                "target": definition["target"],
                "modality": modality,
                "raw_outcome": "active" if label else "inactive",
                "provenance_id": f"fixture://{definition['fixture_id']}/activity/{index + 1}",
            }
        )
        if modality != approved:
            continue
        for replicate in range(2):
            signature_id = f"SIG-{index + 1:04d}-{replicate + 1}"
            transcriptomic_rows.append(
                {
                    "signature_id": signature_id,
                    "compound_id": compound_id,
                    "cell": "CELL_A" if replicate == 0 else "CELL_B",
                    "dose": "1 uM" if replicate == 0 else "10 uM",
                    "time": "24 h" if replicate == 0 else "6 h",
                    "provider": transcriptomic_source_id,
                    "provenance_id": (
                        f"fixture://{definition['fixture_id']}/signature/{signature_id}"
                    ),
                }
            )
            features = {
                f"GENE_{gene + 1:03d}": round(
                    (1 if label else -1) * (0.25 + gene / 20)
                    + math.sin((index + 1) * (gene + 1)) * 0.05
                    + replicate * 0.01,
                    8,
                )
                for gene in range(12)
            }
            expression_rows.append({"signature_id": signature_id, **features})
    return OfflineAssemblyInput(
        fixture_id=definition["fixture_id"],
        activity_rows=activity_rows,
        transcriptomic_rows=transcriptomic_rows,
        expression_rows=expression_rows,
    )


def build_offline_discovery_documents(
    service: WorkflowService, workflow_id: str, modality: str
) -> tuple[
    DiscoveryExecutionLedger,
    SourceCandidateSet,
    HydratedSourceSet,
    CombinationCoverageSet,
    StrategyProposalSet,
]:
    """Create a complete compact discovery inventory backed by one fixture artifact."""

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
            content=json.dumps({"fixture": True, "modality": modality}, sort_keys=True).encode(),
            mime_type="application/json",
            artifact_type="offline_fixture_source_response",
            logical_name=f"offline-{modality}-source-fixture.json",
            producer="offline-fixture-orchestrator",
            idempotency_key=f"offline-{modality}-source-fixture",
        )
    raw = ArtifactReference(
        artifact_id=persisted.id,
        sha256=persisted.sha256,
        artifact_type=persisted.artifact_type,
    )
    records = []
    selected_ids = {activity_task["task_id"], transcriptomic_task["task_id"]}
    for item in plan["provider_specific_query_tasks"]:
        selected = item["task_id"] in selected_ids
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
                pages_or_cursors_attempted=["offline-fixture"],
                search_result_count=1 if selected else 0,
                unique_candidate_count=1 if selected else 0,
                source_response_artifacts=[raw],
                completion_reason="Deterministic offline fixture exhausted.",
            )
        )
    ledger = DiscoveryExecutionLedger(
        workflow_id=workflow_id,
        discovery_round=plan["discovery_round"],
        plan_fingerprint=plan["plan_fingerprint"],
        records=records,
    )
    activity_candidate = SourceCandidate(
        candidate_id=f"candidate-offline-{modality}-activity",
        provider=activity_task["provider"],
        source_identifier=f"offline-{modality}-activity",
        evidence_role=EvidenceRole.ACTIVITY,
        modality=modality,
        discovery_task_id=activity_task["task_id"],
        discovery_query_provenance=[raw],
        provider_dataset_artifacts=[raw],
        candidate_status=CandidateStatus.METADATA_CANDIDATE,
    )
    transcript_candidate = SourceCandidate(
        candidate_id=f"candidate-offline-{modality}-transcriptomic",
        provider=transcriptomic_task["provider"],
        source_identifier=f"offline-{modality}-transcriptomic",
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        discovery_task_id=transcriptomic_task["task_id"],
        discovery_query_provenance=[raw],
        provider_dataset_artifacts=[raw],
        candidate_status=CandidateStatus.METADATA_CANDIDATE,
    )
    candidate_set = SourceCandidateSet(
        workflow_id=workflow_id,
        discovery_round=plan["discovery_round"],
        plan_fingerprint=plan["plan_fingerprint"],
        candidates=[activity_candidate, transcript_candidate],
        provider_dataset_artifacts=[raw],
    )
    compounds = [f"CMPD-{index:04d}" for index in range(1, 37)]
    activity_source = HydratedSource(
        hydrated_source_id=f"hydrated-offline-{modality}-activity",
        source_candidate_id=activity_candidate.candidate_id,
        provider=activity_candidate.provider,
        source_identifier=activity_candidate.source_identifier,
        evidence_role=EvidenceRole.ACTIVITY,
        modality=modality,
        verified_metadata={"compound_ids": compounds},
        compound_index_available=True,
        label_or_activity_fields_available=True,
        source_version="offline-v1",
        completeness_status=HydrationCompleteness.COMPLETE,
        evidence_artifacts=[raw],
    )
    transcript_source = HydratedSource(
        hydrated_source_id=f"hydrated-offline-{modality}-transcriptomic",
        source_candidate_id=transcript_candidate.candidate_id,
        provider=transcript_candidate.provider,
        source_identifier=transcript_candidate.source_identifier,
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        verified_metadata={"compound_ids": compounds},
        compound_index_available=True,
        label_or_activity_fields_available=False,
        experimental_context={"cell": "CELL_A", "dose": "1 uM", "time": "24 h"},
        source_version="offline-v1",
        completeness_status=HydrationCompleteness.COMPLETE,
        evidence_artifacts=[raw],
    )
    hydrated_set = HydratedSourceSet(
        workflow_id=workflow_id,
        discovery_round=plan["discovery_round"],
        sources=[activity_source, transcript_source],
        provider_dataset_artifacts=[raw],
    )
    coverage = CombinationCoverage(
        coverage_id=f"coverage-offline-{modality}",
        activity_source_ids=[activity_source.hydrated_source_id],
        transcriptomic_source_id=transcript_source.hydrated_source_id,
        requested_modality=modality,
        identity_resolution_count=36,
        activity_compound_count=36,
        transcriptomic_compound_count=36,
        overlap_count=36,
        active_count=18,
        inactive_count=18,
        expected_assembled_sample_count=36,
        expected_unique_compound_count=36,
        context=transcript_source.experimental_context,
        computation_provenance=[raw],
    )
    coverage_set = CombinationCoverageSet(
        workflow_id=workflow_id,
        discovery_round=plan["discovery_round"],
        combinations=[coverage],
    )
    proposals = StrategyProposalSet(
        workflow_id=workflow_id,
        discovery_round=plan["discovery_round"],
        proposals=[
            StrategyProposal(
                proposal_id=f"proposal-offline-{modality}",
                included_source_combination=[
                    activity_source.hydrated_source_id,
                    transcript_source.hydrated_source_id,
                ],
                modalities=[modality],
                proposed_context=transcript_source.experimental_context,
                proposed_label_policy={"operator": "source_backed_activity_call"},
                expected_dataset_size=36,
                expected_unique_compounds=36,
                expected_class_balance={"active": 18, "inactive": 18},
                identifier_losses=0,
                scientific_strengths=["Fully provenance-bound offline fixture."],
                scientific_risks=["Synthetic data only."],
                exclusions=["Ambiguous labels"],
                provenance_references=[raw],
                proposal_status=ProposalStatus.VIABLE,
            )
        ],
    )
    return ledger, candidate_set, hydrated_set, coverage_set, proposals


def execute_offline_demo(source_repo_root: Path, output_root: Path, name: str) -> dict[str, Any]:
    """Execute the complete real workflow service path in a disposable local root."""

    definition = load_offline_demo_definition(name)
    output_root = Path(output_root).resolve()
    (output_root / "registry" / "data").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source_repo_root / "registry" / "data" / "operational_provider_capabilities.json",
        output_root / "registry" / "data" / "operational_provider_capabilities.json",
    )
    database = WorkflowDatabase(output_root / "workflow.db")
    database.migrate()
    artifacts = LocalArtifactStore(
        database, output_root / "artifacts", maximum_bytes=4 * 1024 * 1024
    )
    service = WorkflowService(
        database,
        artifacts,
        WorkflowGraph(canonical_workflow_graph_path()),
        repo_root=output_root,
    )

    def summary(build_id: str) -> dict[str, Any]:
        workflow = service.training_dataset_workflow(build_id)
        publication = workflow.get("publication_receipt") or {}
        snapshot = service.get_build(build_id)
        return {
            "build_id": build_id,
            "state": snapshot.current_stage.value,
            "endpoint_id": publication.get("endpoint_id"),
            "artifact_hashes": sorted(
                (item.logical_name, item.sha256) for item in artifacts.list_artifacts(build_id)
            ),
            "network_requests": 0,
            "openai_requests": 0,
            "gctx_network_access": False,
        }

    try:
        build = service.create_build(
            EndpointBuildCreate(
                endpoint_name=definition["endpoint_name"],
                endpoint_slug=definition["endpoint_slug"],
                biological_goal=definition["biological_goal"],
                created_by="offline-demo",
                workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
                benchmark_mode="blind_training_dataset_discovery",
                idempotency_key=f"offline-demo-{name}",
            )
        )
        if build.current_stage.value == "COMPLETED":
            return summary(build.id)
        waiting = service.start_build(
            build.id,
            expected_version=build.version,
            actor="offline-demo",
            idempotency_key=f"offline-demo-{name}-start",
        )
        pending = service.list_approvals(build.id, pending_only=True)
        if not pending:
            modalities = [EndpointSemanticModality(value) for value in definition["modalities"]]
            broad = len(modalities) > 1
            scope = EndpointDiscoveryScope(
                mode=(
                    EndpointDiscoveryMode.BROAD_MODALITY_EXPLORATION
                    if broad
                    else EndpointDiscoveryMode.FIXED_MODALITY
                ),
                biological_target=definition["target"],
                fixed_modality=None if broad else modalities[0],
                candidate_modalities=modalities,
                preserve_modalities_separately=True,
                aggregation_allowed_later=broad,
                selection_deferred_until="assembly_strategy_review" if broad else None,
                scientific_scope=(
                    "Evaluate the requested biological target and preserve each requested "
                    "assay modality independently until explicit human strategy review."
                ),
                provenance=[EndpointDiscoveryScopeProvenance.HUMAN_SCOPED_CONFIGURATION],
            )
            waiting = service.retry_training_dataset_specification(
                build.id,
                expected_version=waiting.version,
                actor="offline-demo-reviewer",
                idempotency_key=f"offline-demo-{name}-scoped-recompile",
                endpoint_discovery_scope=scope,
            )
            pending = service.list_approvals(build.id, pending_only=True)
        approval = pending[0]
        approved = service.decide_approval(
            approval["id"],
            ApprovalDecision(
                decision=ApprovalDecisionValue.APPROVE,
                reviewer_id="offline-demo-reviewer",
                expected_version=waiting.version,
                idempotency_key=f"offline-demo-{name}-specification",
                artifact_hashes=approval["request"]["artifact_hashes"],
            ),
        )
        planned = service.continue_training_dataset_workflow(
            build.id,
            expected_version=approved.version,
            actor="offline-demo",
            idempotency_key=f"offline-demo-{name}-plan",
        )
        authorized = service.authorize_semantics_v2_discovery(
            build.id,
            expected_version=planned.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-discovery-authorization",
        )
        documents = build_offline_discovery_documents(
            service, build.id, definition["approved_modality"]
        )
        reviewed = service.record_semantics_v2_discovery_review(
            build.id,
            ledger=documents[0],
            candidates=documents[1],
            hydrated_sources=documents[2],
            combination_coverage=documents[3],
            strategy_proposals=documents[4],
            expected_version=authorized.version,
            actor="offline-demo",
            idempotency_key=f"offline-demo-{name}-review",
        )
        proposal = documents[4].proposals[0]
        recipe = service.approve_semantics_v2_strategy(
            build.id,
            strategy_proposal_id=proposal.proposal_id,
            approved_modality_aggregation={"operator": "none", "preserve_original": True},
            approved_context_filters=proposal.proposed_context,
            approved_dose_time_rules={"rule": "exact approved context"},
            approved_label_policy=proposal.proposed_label_policy,
            exclusion_rules=proposal.exclusions,
            required_extraction_fields=["compound_id", "signature_id", "provenance"],
            expected_version=reviewed.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-recipe",
        )
        assembled = service.run_approved_dataset_assembly(
            build.id,
            offline_input=build_offline_assembly_input(name),
            expected_version=recipe.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-assembly",
        )
        dataset_approved = service.approve_dataset_for_benchmarking(
            build.id,
            rationale="Offline fixture quality and compound leakage checks passed.",
            expected_version=assembled.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-dataset",
        )
        benchmarked = service.run_endpoint_benchmark(
            build.id,
            expected_version=dataset_approved.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-benchmark",
        )
        selected = service.select_and_validate_endpoint_model(
            build.id,
            candidate_id="candidate-logistic_regression",
            decision_threshold=0.5,
            rationale="Deterministic offline human-selection fixture.",
            expected_version=benchmarked.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-selection",
        )
        completed = service.publish_validated_endpoint(
            build.id,
            expected_version=selected.version,
            actor="offline-demo-reviewer",
            idempotency_key=f"offline-demo-{name}-publication",
        )
        assert completed.current_stage.value == "COMPLETED"
        return summary(build.id)
    finally:
        database.dispose()
