from __future__ import annotations

import pytest
from pydantic import ValidationError

from endoscan_workflows.training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    ActivityRepresentation,
    AssemblyGap,
    AssemblyGapReport,
    AssemblyGraphEdge,
    AssemblyGraphNode,
    BlindBenchmarkInitialContext,
    CapabilityStatus,
    ComponentRole,
    DatasetSpecificationAgentOutcome,
    DiscoveryBeforeStrategyGuard,
    GraphNodeType,
    JoinabilityDiagnostic,
    JoinabilityStatus,
    PredictionUnit,
    PreparationStepStatus,
    SourceCapability,
    SourceValidationStatus,
    StrategyStatus,
    TrainingDatasetAssemblyGraph,
    TrainingDatasetAssemblyStrategy,
    TrainingDatasetPreparationPlan,
    TrainingDatasetPreparationStep,
    TrainingDatasetSpecification,
    TrainingDatasetSpecificationDraft,
    VerifiedSourceInventory,
    VerifiedSourceRecord,
    build_capability_matrix,
    derive_component_requirements,
    materialize_training_dataset_specification,
)

MANDATORY_FIELDS = [
    "canonical_compound_id",
    "preferred_name",
    "canonical_smiles",
    "inchikey",
    "source_compound_ids",
    "transcriptomic_signature",
    "feature_schema",
    "cell_or_tissue_context",
    "dose",
    "exposure_time",
    "transcriptomic_reference_definition",
    "endpoint_activity_value",
    "endpoint_activity_label",
    "endpoint_modality",
    "assay_id",
    "assay_context",
    "provenance",
    "quality_flags",
]


def specification(**changes) -> TrainingDatasetSpecification:
    values = {
        "specification_id": "spec-source-neutral",
        "endpoint_name": "Example receptor antagonist",
        "biological_target": "Example receptor",
        "endpoint_modality": "functional antagonism",
        "endpoint_definition": "Compound-level functional antagonism measured in a reviewed assay.",
        "intended_prediction_task": (
            "Predict continuous or categorized endpoint activity from chemical and "
            "response evidence."
        ),
        "prediction_unit": PredictionUnit.COMPOUND_CONTEXT_DOSE_TIME,
        "acceptable_activity_representations": [
            ActivityRepresentation.CONTINUOUS,
            ActivityRepresentation.BINARY,
        ],
        "acceptable_transcriptomic_representations": ["processed differential signature"],
        "compound_identity_requirements": ["PubChem CID", "InChIKey"],
        "chemical_structure_requirements": ["canonical SMILES", "isomeric SMILES"],
        "experimental_context_requirements": ["cell or tissue", "dose", "exposure time", "control"],
        "mandatory_output_fields": MANDATORY_FIELDS,
        "minimum_evidence_requirements": [
            "official primary activity record",
            "chemical perturbation transcriptomics",
        ],
        "intended_scope_of_claim": (
            "Public-data research model for the explicitly defined endpoint and contexts only."
        ),
        "assumptions_requiring_human_approval": ["activity threshold", "context aggregation"],
    }
    values.update(changes)
    return TrainingDatasetSpecification(**values)


def specification_draft(**changes) -> TrainingDatasetSpecificationDraft:
    values = {
        "schema_version": "1.0.0",
        "endpoint_name": "Example receptor antagonist",
        "biological_target": "Example receptor",
        "endpoint_modality": "functional antagonism",
        "endpoint_definition": "Compound-level functional antagonism in a reviewed assay.",
        "intended_prediction_task": "Predict endpoint activity from chemical response evidence.",
        "candidate_prediction_grain": "compound_cell_context_dose_time",
        "explicit_prediction_grain": None,
        "acceptable_activity_evidence_types": ["continuous_activity"],
        "acceptable_transcriptomic_evidence_types": ["processed differential signature"],
        "compound_identity_requirements": ["PubChem CID", "InChIKey"],
        "chemical_structure_requirements": ["canonical SMILES"],
        "experimental_context_requirements": ["cell", "dose", "time", "control"],
        "mandatory_target_table_fields": MANDATORY_FIELDS,
        "minimum_evidence_requirements": ["official primary public record"],
        "intended_scope_of_claim": "Research use for the explicit endpoint and contexts only.",
        "explicit_ambiguities": ["Permitted contexts require review."],
        "assumptions": [],
        "human_decisions_required": ["Approve the prediction grain."],
    }
    values.update(changes)
    return TrainingDatasetSpecificationDraft(**values)


def test_specification_outcome_enforces_terminal_semantics() -> None:
    completed = DatasetSpecificationAgentOutcome(
        schema_version="1.0.0",
        status="completed",
        specification=specification_draft(),
        requires_human_review=True,
        decision_summary="Review the proposed target contract.",
        unresolved_questions=[],
        limitations=[],
        failure_category=None,
        safe_failure_summary=None,
    )
    assert completed.specification is not None
    for status, category in [
        ("invalid_model_output", "schema_validation_failed"),
        ("model_refused", "model_refusal"),
        ("insufficient_endpoint_definition", "missing_structured_output"),
    ]:
        outcome = DatasetSpecificationAgentOutcome(
            schema_version="1.0.0",
            status=status,
            specification=None,
            requires_human_review=True,
            decision_summary="Human revision is required.",
            unresolved_questions=[],
            limitations=["No valid specification was produced."],
            failure_category=category,
            safe_failure_summary="Structured specification is unavailable.",
        )
        assert outcome.specification is None
    with pytest.raises(ValidationError):
        DatasetSpecificationAgentOutcome(
            schema_version="1.0.0",
            status="invalid_model_output",
            specification=specification_draft(),
            requires_human_review=True,
            decision_summary="Invalid.",
            unresolved_questions=[],
            limitations=[],
            failure_category="schema_validation_failed",
            safe_failure_summary="Invalid.",
        )


def test_draft_materialization_does_not_invent_policy_thresholds() -> None:
    full = materialize_training_dataset_specification(
        specification_draft(), specification_id="spec-approved-deterministically"
    )
    assert full.allowed_missingness == {}
    assert full.minimum_coverage_requirements == {}
    assert full.minimum_class_size_requirements == {}
    assert full.permitted_biological_contexts == []


def source(
    source_id: str,
    roles: list[ComponentRole],
    *,
    fields: list[str] | None = None,
) -> VerifiedSourceRecord:
    fields = fields or ["canonical_compound_id"]
    return VerifiedSourceRecord(
        source_id=source_id,
        source_roles=roles,
        source_system="synthetic official adapter",
        stable_accession=f"ACC-{source_id}",
        official_source="https://official.invalid/metadata",
        verified_public_availability=True,
        capabilities=[
            SourceCapability(
                component=role,
                status=CapabilityStatus.VERIFIED_AVAILABLE,
                fields=fields,
                evidence_references=[f"artifact:{source_id}#metadata"],
            )
            for role in roles
        ],
        identifier_fields=["canonical_compound_id"],
        structure_fields=["canonical_smiles"] if ComponentRole.CHEMICAL_STRUCTURE in roles else [],
        measurement_fields=["activity"] if ComponentRole.ENDPOINT_ACTIVITY in roles else [],
        experimental_context_fields=["dose", "time"],
        access_status=CapabilityStatus.VERIFIED_AVAILABLE,
        licence_status=CapabilityStatus.METADATA_ONLY,
        source_references=[f"artifact:{source_id}"],
        evidence_quality="Synthetic source-neutral contract fixture.",
        validation_status=SourceValidationStatus.VERIFIED,
        artifact_hashes=["0" * 64],
    )


def inventory(*sources: VerifiedSourceRecord, version: int = 1) -> VerifiedSourceInventory:
    return VerifiedSourceInventory(
        inventory_id="inventory-general",
        version=version,
        specification_id="spec-source-neutral",
        sources=list(sources),
    )


def final_graph(
    inv: VerifiedSourceInventory, source_ids: list[str]
) -> TrainingDatasetAssemblyGraph:
    nodes = [
        AssemblyGraphNode(
            node_id=f"source-{index}",
            node_type=GraphNodeType.SOURCE,
            label=source_id,
            source_id=source_id,
            source_roles=next(
                item.source_roles for item in inv.sources if item.source_id == source_id
            ),
            produces_fields=["canonical_compound_id"],
        )
        for index, source_id in enumerate(source_ids, start=1)
    ]
    nodes.append(
        AssemblyGraphNode(
            node_id="final",
            node_type=GraphNodeType.FINAL_CANDIDATE_TABLE,
            label="Candidate training table",
            produces_fields=MANDATORY_FIELDS,
        )
    )
    return TrainingDatasetAssemblyGraph(
        graph_id="graph-general",
        inventory_id=inv.inventory_id,
        inventory_version=inv.version,
        target_specification_id=inv.specification_id,
        target_fields=MANDATORY_FIELDS,
        nodes=nodes,
        edges=[
            AssemblyGraphEdge(
                from_node=f"source-{index}",
                to_node="final",
                transferred_fields=["canonical_compound_id"],
                join_keys=["canonical_compound_id"],
            )
            for index in range(1, len(source_ids) + 1)
        ],
    )


def test_binary_and_continuous_endpoint_contracts_are_supported() -> None:
    binary = specification(acceptable_activity_representations=[ActivityRepresentation.BINARY])
    continuous = specification(
        acceptable_activity_representations=[ActivityRepresentation.CONTINUOUS]
    )
    assert binary.acceptable_activity_representations == [ActivityRepresentation.BINARY]
    assert continuous.acceptable_activity_representations == [ActivityRepresentation.CONTINUOUS]


def test_ambiguous_endpoint_remains_explicitly_unresolved() -> None:
    value = specification(
        biological_target="unresolved",
        endpoint_modality="ambiguous",
        unresolved_questions=["Which target and functional modality define this endpoint?"],
    )
    assert value.requires_human_review is True
    assert value.unresolved_questions


def test_multiple_observation_grains_and_explicit_other() -> None:
    for unit in (
        PredictionUnit.COMPOUND,
        PredictionUnit.COMPOUND_CONTEXT,
        PredictionUnit.COMPOUND_CONTEXT_DOSE_TIME,
        PredictionUnit.AGGREGATED_COMPOUND_PROFILE,
    ):
        assert specification(prediction_unit=unit).prediction_unit is unit
    with pytest.raises(ValidationError):
        specification(prediction_unit=PredictionUnit.EXPLICIT_OTHER)
    assert specification(
        prediction_unit=PredictionUnit.EXPLICIT_OTHER,
        explicit_prediction_grain="one row per compound and reviewed assay context",
    ).explicit_prediction_grain


def test_missing_identity_requirement_or_output_is_rejected() -> None:
    with pytest.raises(ValidationError):
        specification(compound_identity_requirements=[])
    with pytest.raises(ValidationError):
        specification(mandatory_output_fields=["transcriptomic_signature", "canonical_smiles"])


def test_component_requirements_are_derived_before_any_strategy() -> None:
    requirements = derive_component_requirements(specification())
    roles = {item.role for item in requirements.requirements if item.mandatory}
    assert ComponentRole.ENDPOINT_ACTIVITY in roles
    assert ComponentRole.TRANSCRIPTOMIC_MATRIX in roles
    assert ComponentRole.COMPOUND_IDENTITY in roles
    assert ComponentRole.CHEMICAL_STRUCTURE in roles
    assert ComponentRole.SOURCE_ID_MAPPING in {item.role for item in requirements.requirements}


def test_one_source_can_supply_several_roles_and_matrix_is_deterministic() -> None:
    multi = source(
        "multi-role",
        [
            ComponentRole.ENDPOINT_ACTIVITY,
            ComponentRole.COMPOUND_IDENTITY,
            ComponentRole.CHEMICAL_STRUCTURE,
        ],
    )
    inv = inventory(multi)
    matrix = build_capability_matrix(inv, derive_component_requirements(specification()))
    assert sum(cell.status is CapabilityStatus.VERIFIED_AVAILABLE for cell in matrix.cells) == 3
    assert any(cell.status is CapabilityStatus.UNAVAILABLE for cell in matrix.cells)


def test_several_sources_can_supply_one_role() -> None:
    inv = inventory(
        source("activity-a", [ComponentRole.ENDPOINT_ACTIVITY]),
        source("activity-b", [ComponentRole.ENDPOINT_ACTIVITY]),
    )
    matrix = build_capability_matrix(inv, derive_component_requirements(specification()))
    activity = [cell for cell in matrix.cells if cell.component is ComponentRole.ENDPOINT_ACTIVITY]
    assert len(activity) == 2
    assert all(cell.status is CapabilityStatus.VERIFIED_AVAILABLE for cell in activity)


def test_planner_cannot_run_before_inventory_and_matrix() -> None:
    spec = specification()
    requirements = derive_component_requirements(spec)
    with pytest.raises(ValueError, match="inventory"):
        DiscoveryBeforeStrategyGuard.validate(spec, requirements, None, None)


def test_planner_guard_accepts_only_matching_inventory_and_matrix() -> None:
    spec = specification()
    requirements = derive_component_requirements(spec)
    inv = inventory(source("activity", [ComponentRole.ENDPOINT_ACTIVITY]))
    matrix = build_capability_matrix(inv, requirements)
    DiscoveryBeforeStrategyGuard.validate(spec, requirements, inv, matrix)
    stale = matrix.model_copy(update={"inventory_version": 2})
    with pytest.raises(ValueError, match="stale"):
        DiscoveryBeforeStrategyGuard.validate(spec, requirements, inv, stale)


@pytest.mark.parametrize("source_count", [1, 2, 4])
def test_general_graph_supports_one_two_or_many_sources(source_count: int) -> None:
    values = [
        source(f"source-{index}", [ComponentRole.ENDPOINT_ACTIVITY])
        for index in range(source_count)
    ]
    inv = inventory(*values)
    graph = final_graph(inv, [item.source_id for item in values])
    graph.validate_inventory(inv)
    assert sum(node.node_type is GraphNodeType.SOURCE for node in graph.nodes) == source_count


def test_branching_graph_and_separate_identity_source_are_supported() -> None:
    inv = inventory(
        source("activity-a", [ComponentRole.ENDPOINT_ACTIVITY]),
        source("activity-b", [ComponentRole.ENDPOINT_ACTIVITY]),
        source("transcriptomics", [ComponentRole.TRANSCRIPTOMIC_MATRIX]),
        source("identity", [ComponentRole.COMPOUND_IDENTITY, ComponentRole.CHEMICAL_STRUCTURE]),
    )
    graph = final_graph(inv, sorted(inv.source_ids))
    graph.validate_inventory(inv)


def test_cyclic_graph_is_rejected() -> None:
    inv = inventory(source("activity", [ComponentRole.ENDPOINT_ACTIVITY]))
    graph = final_graph(inv, ["activity"])
    with pytest.raises(ValidationError, match="acyclic"):
        TrainingDatasetAssemblyGraph.model_validate(
            {
                **graph.model_dump(mode="json"),
                "edges": [
                    {"from_node": "source-1", "to_node": "final"},
                    {"from_node": "final", "to_node": "source-1"},
                ],
            }
        )


def test_missing_target_field_is_rejected() -> None:
    inv = inventory(source("activity", [ComponentRole.ENDPOINT_ACTIVITY]))
    graph = final_graph(inv, ["activity"])
    payload = graph.model_dump(mode="json")
    payload["nodes"][-1]["produces_fields"] = ["canonical_compound_id"]
    with pytest.raises(ValidationError, match="target fields"):
        TrainingDatasetAssemblyGraph.model_validate(payload)


def test_undiscovered_source_reference_is_rejected() -> None:
    inv = inventory(source("activity", [ComponentRole.ENDPOINT_ACTIVITY]))
    graph = final_graph(inv, ["activity"])
    payload = graph.model_dump(mode="json")
    payload["nodes"][0]["source_id"] = "invented-source"
    invented = TrainingDatasetAssemblyGraph.model_validate(payload)
    with pytest.raises(ValueError, match="undiscovered"):
        invented.validate_inventory(inv)


def test_joinability_exact_partial_and_requires_download_are_distinct() -> None:
    exact = JoinabilityDiagnostic(
        diagnostic_id="join-exact",
        status=JoinabilityStatus.COMPUTED_EXACT,
        source_ids=["a", "b"],
        exact_overlap_count=12,
    )
    partial = JoinabilityDiagnostic(
        diagnostic_id="join-partial",
        status=JoinabilityStatus.COMPUTED_PARTIAL,
        source_ids=["a", "b"],
        partial_overlap_count=4,
    )
    download = JoinabilityDiagnostic(
        diagnostic_id="join-download",
        status=JoinabilityStatus.REQUIRES_DOWNLOAD,
        source_ids=["a", "b"],
        required_downloads=["activity result table"],
    )
    assert exact.exact_overlap_count == 12
    assert partial.exact_overlap_count is None
    assert download.required_downloads


def test_metadata_only_overlap_cannot_claim_an_exact_count() -> None:
    with pytest.raises(ValidationError, match="exact overlap"):
        JoinabilityDiagnostic(
            diagnostic_id="join-metadata",
            status=JoinabilityStatus.METADATA_ONLY,
            source_ids=["a", "b"],
            exact_overlap_count=100,
        )


def test_gap_directed_discovery_is_bounded_and_deduplicated() -> None:
    report = AssemblyGapReport(
        report_id="gaps-v1",
        inventory_id="inventory-general",
        inventory_version=1,
        discovery_round=0,
        maximum_discovery_rounds=2,
        gaps=[
            AssemblyGap(
                gap_id="missing-identity",
                component=ComponentRole.SOURCE_ID_MAPPING,
                description="No deterministic compound identity bridge is available.",
                blocking=True,
                targeted_search_request="find official compound identity mapping",
            ),
            AssemblyGap(
                gap_id="missing-transcriptomics",
                component=ComponentRole.TRANSCRIPTOMIC_MATRIX,
                description="Additional chemical perturbation resource is required.",
                blocking=True,
                targeted_search_request="find broad chemical perturbation transcriptomics",
            ),
        ],
    )
    assert report.next_queries({"find official compound identity mapping"}) == [
        "find broad chemical perturbation transcriptomics"
    ]
    exhausted = report.model_copy(update={"discovery_round": 2})
    assert exhausted.next_queries(set()) == []


def test_preparation_plan_requires_contiguous_order() -> None:
    with pytest.raises(ValidationError, match="contiguous"):
        TrainingDatasetPreparationPlan(
            plan_id="plan-invalid",
            strategy_id="strategy",
            steps=[
                TrainingDatasetPreparationStep(
                    step_id="one",
                    order=1,
                    action="Retrieve activity metadata",
                    status=PreparationStepStatus.DETERMINISTIC_READY,
                ),
                TrainingDatasetPreparationStep(
                    step_id="three",
                    order=3,
                    action="Join source records",
                    status=PreparationStepStatus.REQUIRES_COMPUTATION,
                ),
            ],
        )


def test_strategy_binds_inventory_graph_joinability_and_plan() -> None:
    inv = inventory(source("activity", [ComponentRole.ENDPOINT_ACTIVITY]))
    graph = final_graph(inv, ["activity"])
    diagnostic = JoinabilityDiagnostic(
        diagnostic_id="join-download",
        status=JoinabilityStatus.REQUIRES_DOWNLOAD,
        source_ids=["activity"],
        required_downloads=["result table"],
    )
    plan = TrainingDatasetPreparationPlan(
        plan_id="plan-source-neutral",
        strategy_id="strategy-source-neutral",
        steps=[
            TrainingDatasetPreparationStep(
                step_id="retrieve",
                order=1,
                action="Retrieve the reviewed result table",
                status=PreparationStepStatus.REQUIRES_DOWNLOAD,
            )
        ],
    )
    strategy = TrainingDatasetAssemblyStrategy(
        strategy_id="strategy-source-neutral",
        target_specification_id=inv.specification_id,
        source_inventory_id=inv.inventory_id,
        source_inventory_version=inv.version,
        source_graph=graph,
        source_roles={"activity": [ComponentRole.ENDPOINT_ACTIVITY]},
        identity_policy="Use deterministic canonical identifiers and retain conflicts.",
        chemical_standardization_policy="Flag mixtures, normalize salts, and preserve provenance.",
        label_policy="Do not create labels until reviewed activity records are ingested.",
        transcriptomic_condition_policy="Keep cell, dose and time contexts separate.",
        repeated_signature_policy="Retain condition provenance before any aggregation.",
        expected_output_grain="one row per compound and context",
        overlap_diagnostic=diagnostic,
        evidence_quality="Official metadata only; source ingestion is still required.",
        preparation_effort="requires bounded source ingestion",
        missing_components=[ComponentRole.TRANSCRIPTOMIC_MATRIX],
        preparation_plan=plan,
        status=StrategyStatus.REQUIRES_ADDITIONAL_DISCOVERY,
    )
    strategy.validate_inventory(inv)


def test_blind_context_contains_no_endpoint_specific_hints() -> None:
    context = BlindBenchmarkInitialContext(
        benchmark_mode=BLIND_TRAINING_DATASET_DISCOVERY,
        endpoint_name="Example endpoint",
        biological_goal="Construct a public-data training dataset without source hints.",
        target_training_dataset_contract={"required_fields": MANDATORY_FIELDS},
        source_adapter_capabilities=["official structured source adapters"],
        approved_scientific_policies=["discovery before strategy"],
        allowed_tools=["search_activity_sources"],
        planner_provider="fake",
        planner_model="planner-fixture",
        worker_provider="fake",
        worker_model="worker-fixture",
        budgets={"provider_retries": 0},
    )
    assert context.source_hints == []
    assert context.article_hint is None
    assert context.assay_id_hint is None


def test_blind_context_rejects_source_hints() -> None:
    with pytest.raises(ValidationError):
        BlindBenchmarkInitialContext(
            benchmark_mode=BLIND_TRAINING_DATASET_DISCOVERY,
            endpoint_name="Example endpoint",
            biological_goal="Construct a public-data training dataset without source hints.",
            target_training_dataset_contract={"required_fields": MANDATORY_FIELDS},
            planner_provider="fake",
            planner_model="planner-fixture",
            worker_provider="fake",
            worker_model="worker-fixture",
            budgets={"provider_retries": 0},
            source_hints=["known-source"],
        )
