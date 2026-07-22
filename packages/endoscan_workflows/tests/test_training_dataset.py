from __future__ import annotations

import pytest
from pydantic import ValidationError

from endoscan_workflows.training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    SPECIALIZED_AGENT_SEQUENCE,
    ActivityRepresentation,
    AssemblyGap,
    AssemblyGapReport,
    AssemblyGraphEdge,
    AssemblyGraphNode,
    BlindBenchmarkInitialContext,
    CandidateEndpointConstructionDiagnostic,
    CapabilityStatus,
    ComponentRole,
    DatasetSpecificationAgentOutcome,
    DatasetSpecificationCompiler,
    DatasetSpecificationReviewOutcome,
    DatasetSpecificationSuggestedCorrection,
    DiscoveryBeforeStrategyGuard,
    EndpointDiscoveryMode,
    EndpointDiscoveryScope,
    EndpointDiscoveryScopeProvenance,
    EndpointSemanticModality,
    GraphNodeType,
    JoinabilityDiagnostic,
    JoinabilityStatus,
    ModalityAggregationApprovalStatus,
    ModalityAggregationOperator,
    ModalityAggregationPolicy,
    ModalityJoinabilityDiagnostic,
    PossibleSourceDuplicateGroup,
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
    TrainingDatasetSpecificationApprovalPolicy,
    TrainingDatasetSpecificationDraft,
    VerifiedSourceInventory,
    VerifiedSourceRecord,
    build_capability_matrix,
    derive_component_requirements,
    derive_endpoint_request_semantic_hints,
    materialize_training_dataset_specification,
    specification_question_kind,
    validate_dataset_specification_semantics,
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
        "compound_identity_requirements": ["canonical compound identifier", "InChIKey"],
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
        "compound_identity_requirements": ["canonical compound identifier", "InChIKey"],
        "chemical_structure_requirements": ["canonical SMILES"],
        "experimental_context_requirements": ["cell", "dose", "time", "control"],
        "mandatory_target_table_fields": MANDATORY_FIELDS,
        "minimum_evidence_requirements": ["official primary public record"],
        "intended_scope_of_claim": "Research use for the explicit endpoint and contexts only.",
        "explicit_exclusions": ["Adjacent endpoint modalities"],
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
        blocking_questions=[],
        approval_questions=["Approve the prediction grain."],
        missing_core_elements=[],
        unresolved_questions=[],
        limitations=[],
        failure_category=None,
        safe_failure_summary=None,
    )
    assert completed.specification is not None
    for status, category, missing, blocking in [
        ("invalid_model_output", "schema_validation_failed", [], []),
        ("model_refused", "model_refusal", [], []),
        (
            "insufficient_endpoint_definition",
            None,
            ["biological_target_or_process"],
            ["Which biological target is intended?"],
        ),
    ]:
        outcome = DatasetSpecificationAgentOutcome(
            schema_version="1.0.0",
            status=status,
            specification=None,
            requires_human_review=True,
            decision_summary="Human revision is required.",
            blocking_questions=blocking,
            approval_questions=[],
            missing_core_elements=missing,
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
            blocking_questions=[],
            approval_questions=[],
            missing_core_elements=[],
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


def test_human_policy_materializes_nullable_and_population_semantics() -> None:
    policy = TrainingDatasetSpecificationApprovalPolicy(
        activity_representation="Retain measured values and separately approved derived labels.",
        observation_grain="Keep compound by transcriptomic context observations distinct.",
        evidence_hierarchy="Require direct measured endpoint activity as primary evidence.",
        multiple_activity_assays="Keep assay-specific activity records separate during discovery.",
        conflicting_activity_records=(
            "Preserve conflicts with provenance and do not resolve them yet."
        ),
        transcriptomic_contexts="Keep different cells, doses, durations and controls distinct.",
        repeated_transcriptomic_signatures=(
            "Retain repeated signatures until a later approved policy."
        ),
        quality_thresholds="Set no numeric threshold before inspecting source distributions.",
        minimum_usable_coverage="Set no count or overlap minimum before joinability diagnostics.",
        missingness="Allow explicit staging missingness but enforce finalized table constraints.",
        mandatory_output_fields=MANDATORY_FIELDS,
        nullable_output_fields=[
            "preferred_name",
            "endpoint_activity_value",
            "endpoint_activity_label",
        ],
        optional_output_fields=["isomeric_smiles"],
        population_constraints=[
            "At least one activity value or label is populated for every usable observation."
        ],
    )
    full = materialize_training_dataset_specification(
        specification_draft(),
        specification_id="spec-approved-with-policy",
        approved_policy=policy,
    )
    assert full.approved_policy_version == "1.0.0"
    assert len(full.approved_policy_decisions) == 10
    assert full.nullable_output_fields == policy.nullable_output_fields
    assert full.optional_output_fields == ["isomeric_smiles"]
    assert full.population_constraints == policy.population_constraints
    assert full.allowed_missingness == {}
    assert full.minimum_coverage_requirements == {}
    assert full.requires_human_review is False


def test_component_requirements_are_complete_and_source_neutral() -> None:
    requirements = derive_component_requirements(specification())
    assert len(requirements.requirements) == 11
    roles = {item.role for item in requirements.requirements}
    assert {
        ComponentRole.ENDPOINT_ACTIVITY,
        ComponentRole.SAMPLE_METADATA,
        ComponentRole.COMPOUND_IDENTITY,
        ComponentRole.CHEMICAL_STRUCTURE,
        ComponentRole.TRANSCRIPTOMIC_MATRIX,
        ComponentRole.PROVENANCE_LICENSE,
    }.issubset(roles)
    assert all(item.acceptable_data_forms for item in requirements.requirements)
    assert all(item.explicit_exclusions for item in requirements.requirements)
    assert all(item.unresolved_discovery_questions for item in requirements.requirements)


@pytest.mark.parametrize(
    ("endpoint", "target", "modality"),
    [
        ("X receptor antagonist", "X receptor", EndpointSemanticModality.ANTAGONISM),
        ("Y receptor agonist", "Y receptor", EndpointSemanticModality.AGONISM),
        ("Z enzyme inhibitor", "Z enzyme", EndpointSemanticModality.INHIBITION),
        ("A receptor binding", "A receptor", EndpointSemanticModality.BINDING),
    ],
)
def test_semantic_hints_recognize_sufficient_generic_endpoints(
    endpoint: str, target: str, modality: EndpointSemanticModality
) -> None:
    hints = derive_endpoint_request_semantic_hints(
        endpoint,
        "Construct a compound-level prediction training dataset.",
    )
    assert hints.core_definition_status == "sufficient"
    assert hints.explicit_target_terms == [target]
    assert hints.explicit_modality_terms == [modality]
    assert hints.missing_core_elements == []


@pytest.mark.parametrize(
    ("endpoint", "expected_missing"),
    [
        ("receptor activity", "biological_target_or_process"),
        ("toxicity", "biological_target_or_process"),
        ("hormonal effects", "biological_target_or_process"),
    ],
)
def test_semantic_hints_expose_exact_missing_core_elements(
    endpoint: str, expected_missing: str
) -> None:
    hints = derive_endpoint_request_semantic_hints(
        endpoint,
        "Construct a compound-level prediction training dataset.",
    )
    assert hints.core_definition_status == "insufficient"
    assert expected_missing in hints.missing_core_elements


def test_semantic_hints_preserve_multiple_modalities_for_later_policy_review() -> None:
    hints = derive_endpoint_request_semantic_hints(
        "X receptor antagonist and agonist",
        "Construct a compound-level prediction training dataset.",
    )
    assert hints.core_definition_status == "sufficient"
    assert hints.explicit_modality_terms == [
        EndpointSemanticModality.ANTAGONISM,
        EndpointSemanticModality.AGONISM,
    ]
    assert "contradictory_core_definition" not in hints.missing_core_elements


@pytest.mark.parametrize(
    "question",
    [
        "Should activity be binary versus continuous?",
        "Which observation grain should be used?",
        "What minimum evidence threshold is required?",
        "Which aggregation policy should be used?",
    ],
)
def test_dataset_policy_questions_are_approval_only(question: str) -> None:
    assert specification_question_kind(question) == "approval"


@pytest.mark.parametrize(
    "question",
    [
        "Which receptor is intended?",
        "Is agonism or antagonism requested?",
        "Is the endpoint receptor activity or a downstream phenotype?",
    ],
)
def test_core_endpoint_questions_remain_blocking(question: str) -> None:
    assert specification_question_kind(question) == "blocking"


def test_semantic_validator_flags_policy_only_null_draft_without_fabricating_one() -> None:
    hints = derive_endpoint_request_semantic_hints(
        "X receptor antagonist",
        "Construct a compound-level prediction dataset with transcriptomic responses.",
    )
    outcome = DatasetSpecificationAgentOutcome(
        schema_version="1.0.0",
        status="insufficient_endpoint_definition",
        specification=None,
        requires_human_review=True,
        decision_summary="Construction policies remain unresolved.",
        blocking_questions=["Should activity be binary versus continuous?"],
        approval_questions=[],
        missing_core_elements=["requested_modality_or_prediction_claim"],
        unresolved_questions=["Which observation grain should be used?"],
        limitations=[],
        failure_category=None,
        safe_failure_summary=None,
    )
    validation = validate_dataset_specification_semantics(hints, outcome)
    assert validation.status == "semantic_contract_violation"
    assert validation.violation_code == "non_blocking_policy_treated_as_core_missing"
    assert validation.requires_explicit_human_rerun is True
    assert outcome.specification is None


def test_semantic_validator_permits_completed_draft_with_approval_questions() -> None:
    hints = derive_endpoint_request_semantic_hints(
        "X receptor antagonist",
        "Construct a compound training dataset with transcriptomic responses.",
    )
    outcome = DatasetSpecificationAgentOutcome(
        schema_version="1.0.0",
        status="completed",
        specification=specification_draft(
            endpoint_name="X receptor antagonist",
            biological_target="X receptor",
            endpoint_modality="antagonism",
            endpoint_definition="Compound-level X receptor antagonism.",
        ),
        requires_human_review=True,
        decision_summary="Review the source-neutral draft.",
        blocking_questions=[],
        approval_questions=["Should activity be binary versus continuous?"],
        missing_core_elements=[],
        unresolved_questions=[],
        limitations=[],
        failure_category=None,
        safe_failure_summary=None,
    )
    validation = validate_dataset_specification_semantics(hints, outcome)
    assert validation.status == "valid"
    assert validation.requires_explicit_human_rerun is False


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


def test_joinability_reports_modalities_before_candidate_union() -> None:
    diagnostic = JoinabilityDiagnostic(
        diagnostic_id="join-functional-options",
        status=JoinabilityStatus.COMPUTED_EXACT,
        source_ids=["agonist-assay", "antagonist-assay", "transcriptomics"],
        exact_overlap_count=18,
        modality_diagnostics=[
            ModalityJoinabilityDiagnostic(
                modality=EndpointSemanticModality.AGONISM,
                activity_compound_count=30,
                transcriptomic_overlap_count=12,
                active_count=8,
                inactive_count=22,
            ),
            ModalityJoinabilityDiagnostic(
                modality=EndpointSemanticModality.ANTAGONISM,
                activity_compound_count=35,
                transcriptomic_overlap_count=14,
                active_count=9,
                inactive_count=26,
                conflicting_outcome_count=2,
            ),
            ModalityJoinabilityDiagnostic(
                modality=EndpointSemanticModality.BINDING,
                activity_compound_count=40,
                transcriptomic_overlap_count=16,
            ),
        ],
        candidate_endpoint_diagnostics=[
            CandidateEndpointConstructionDiagnostic(
                candidate_name="functional agonism-or-antagonism",
                source_modalities=[
                    EndpointSemanticModality.AGONISM,
                    EndpointSemanticModality.ANTAGONISM,
                ],
                activity_compound_count=51,
                cross_modality_overlap_count=14,
                transcriptomic_overlap_count=18,
                active_count=15,
                inactive_count=36,
                conflicting_outcome_count=2,
            )
        ],
        possible_duplicate_groups=[
            PossibleSourceDuplicateGroup(
                source_ids=["agonist-assay", "antagonist-assay"],
                shared_identifiers=["canonical compound identifier"],
            )
        ],
    )
    assert diagnostic.modality_diagnostics[2].modality is EndpointSemanticModality.BINDING
    assert EndpointSemanticModality.BINDING not in (
        diagnostic.candidate_endpoint_diagnostics[0].source_modalities
    )
    assert diagnostic.possible_duplicate_groups[0].resolution_status == "unresolved"


def functional_union_policy(**changes) -> ModalityAggregationPolicy:
    values = {
        "policy_id": "policy-functional-receptor-v1",
        "target": "Example receptor",
        "derived_endpoint_name": "functional receptor activity",
        "source_modalities": ["agonism", "antagonism"],
        "included_assay_roles": ["functional agonist assay", "functional antagonist assay"],
        "excluded_modalities": ["binding"],
        "aggregation_operator": "any_of",
        "active_rule": "Active when either approved functional modality is active.",
        "inactive_rule": "Inactive when both observed functional modalities are inactive.",
        "inconclusive_rule": "Retain inconclusive when observed evidence is inconclusive.",
        "conflict_rule": "Flag conflicting outcomes and retain every source assay record.",
        "missing_assay_rule": "Do not infer a missing modality; apply the approved coverage rule.",
        "provenance_requirements": [
            "retain original assay identifier, modality, outcome and immutable artifact"
        ],
        "scientific_rationale": (
            "Both modalities measure functional interaction while direction-specific evidence "
            "remains independently reviewable."
        ),
        "human_approval_status": "proposed",
        "policy_version": "1.0.0",
    }
    values.update(changes)
    return ModalityAggregationPolicy(**values)


def test_human_approved_functional_union_is_permitted_but_not_universal() -> None:
    proposed = functional_union_policy()
    assert proposed.aggregation_operator is ModalityAggregationOperator.ANY_OF
    assert proposed.can_construct_labels is False
    assert EndpointSemanticModality.BINDING in proposed.excluded_modalities

    approved = functional_union_policy(
        human_approval_status="approved",
        approval_artifact_id="art-aggregation-approval",
        approval_artifact_hash="a" * 64,
    )
    assert approved.human_approval_status is ModalityAggregationApprovalStatus.APPROVED
    assert approved.can_construct_labels is True

    modality_specific = functional_union_policy(
        policy_id="policy-binding-only-v1",
        derived_endpoint_name="receptor binding",
        source_modalities=["binding"],
        included_assay_roles=["receptor binding assay"],
        excluded_modalities=["agonism", "antagonism"],
        aggregation_operator="modality_specific",
        active_rule="Active only under the approved binding threshold.",
        inactive_rule="Inactive only under the approved binding threshold.",
        scientific_rationale=(
            "This endpoint predicts binding and makes no functional activity claim."
        ),
    )
    assert modality_specific.source_modalities != proposed.source_modalities
    assert modality_specific.can_construct_labels is False


def test_approved_aggregation_requires_immutable_human_approval_evidence() -> None:
    with pytest.raises(ValidationError, match="immutable approval evidence"):
        functional_union_policy(human_approval_status="approved")


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
        class_counts={"active": 3},
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
        modality_aggregation_policy=functional_union_policy(),
        transcriptomic_condition_policy="Keep cell, dose and time contexts separate.",
        repeated_signature_policy="Retain condition provenance before any aggregation.",
        expected_output_grain="one row per compound and context",
        overlap_diagnostic=diagnostic,
        expected_coverage={"activity": 0.8},
        expected_class_balance={"active": 3},
        evidence_quality="Official metadata only; source ingestion is still required.",
        preparation_effort="requires bounded source ingestion",
        missing_components=[ComponentRole.TRANSCRIPTOMIC_MATRIX],
        preparation_plan=plan,
        status=StrategyStatus.REQUIRES_ADDITIONAL_DISCOVERY,
    )
    strategy.validate_inventory(inv)
    assert strategy.source_roles[0].source_id == "activity"
    assert strategy.expected_coverage[0].name == "activity"
    assert strategy.expected_class_balance[0].name == "active"
    assert strategy.overlap_diagnostic.class_counts[0].label == "active"
    assert strategy.modality_aggregation_policy is not None
    assert strategy.modality_aggregation_policy.can_construct_labels is False
    assert strategy.requires_human_review is True


def test_blind_context_contains_no_endpoint_specific_hints() -> None:
    context = BlindBenchmarkInitialContext(
        benchmark_mode=BLIND_TRAINING_DATASET_DISCOVERY,
        endpoint_name="Example endpoint",
        biological_goal="Construct a public-data training dataset without source hints.",
        target_training_dataset_contract={"required_fields": MANDATORY_FIELDS},
        source_adapter_capabilities=["official structured source adapters"],
        approved_scientific_policies=["discovery before strategy"],
        allowed_tools=["search_activity_sources"],
        planner_provider="offline_fixture",
        planner_model="planner-fixture",
        worker_provider="offline_fixture",
        worker_model="worker-fixture",
        budgets={"provider_retries": 0},
    )
    assert context.source_hints == []
    assert context.article_hint is None
    assert context.assay_id_hint is None


def test_discovery_agents_expose_both_reviewed_source_families() -> None:
    agents = {item.agent_name: item for item in SPECIALIZED_AGENT_SEQUENCE}
    assert {
        "search_activity_sources",
        "search_epa_assays",
        "inspect_epa_assay_metadata",
    }.issubset(agents["Activity Evidence Discovery Agent"].allowed_tools)
    assert {
        "search_transcriptomic_sources",
        "search_lincs_resources",
        "inspect_lincs_signature_metadata",
    }.issubset(agents["Transcriptomic Evidence Discovery Agent"].allowed_tools)


def test_blind_context_rejects_source_hints() -> None:
    with pytest.raises(ValidationError):
        BlindBenchmarkInitialContext(
            benchmark_mode=BLIND_TRAINING_DATASET_DISCOVERY,
            endpoint_name="Example endpoint",
            biological_goal="Construct a public-data training dataset without source hints.",
            target_training_dataset_contract={"required_fields": MANDATORY_FIELDS},
            planner_provider="offline_fixture",
            planner_model="planner-fixture",
            worker_provider="offline_fixture",
            worker_model="worker-fixture",
            budgets={"provider_retries": 0},
            source_hints=["known-source"],
        )


@pytest.mark.parametrize(
    ("endpoint_name", "goal", "target", "modality"),
    [
        (
            "X receptor antagonist",
            "Construct a compound-level training dataset with transcriptomic responses.",
            "X receptor",
            "antagonism",
        ),
        (
            "Y receptor agonist",
            "Construct compound training data with transcriptomic responses.",
            "Y receptor",
            "agonism",
        ),
        (
            "Z enzyme inhibitor",
            "Construct compound-level transcriptomic training data.",
            "Z enzyme",
            "inhibition",
        ),
        (
            "A receptor binding",
            "Construct compound-level transcriptomic training data.",
            "A receptor",
            "binding",
        ),
    ],
)
def test_dataset_specification_compiler_handles_explicit_core_requests(
    endpoint_name: str,
    goal: str,
    target: str,
    modality: str,
) -> None:
    hints = derive_endpoint_request_semantic_hints(endpoint_name, goal)
    outcome = DatasetSpecificationCompiler().compile(
        endpoint_name=endpoint_name,
        biological_goal=goal,
        semantic_hints=hints,
        target_training_dataset_contract={"required_fields": MANDATORY_FIELDS},
        approved_platform_policies=["source-neutral specification"],
    )
    assert outcome.status == "compiled"
    assert outcome.provider_invocations == 0
    assert outcome.specification is not None
    assert outcome.specification.biological_target == target
    assert outcome.specification.endpoint_modality == modality
    assert outcome.specification.blocking_questions == []


@pytest.mark.parametrize(
    ("endpoint_name", "goal", "missing"),
    [
        (
            "toxicity antagonist",
            "Construct compound-level transcriptomic training data.",
            "biological_target_or_process",
        ),
        (
            "X receptor",
            "Construct compound-level transcriptomic training data.",
            "requested_modality_or_prediction_claim",
        ),
        (
            "X receptor antagonist",
            "Explore public data.",
            "compound_level_prediction_objective",
        ),
    ],
)
def test_dataset_specification_compiler_requires_exact_missing_core_fields(
    endpoint_name: str,
    goal: str,
    missing: str,
) -> None:
    hints = derive_endpoint_request_semantic_hints(endpoint_name, goal)
    outcome = DatasetSpecificationCompiler().compile(
        endpoint_name=endpoint_name,
        biological_goal=goal,
        semantic_hints=hints,
        target_training_dataset_contract={},
        approved_platform_policies=[],
    )
    assert outcome.status == "needs_human_clarification"
    assert outcome.specification is None
    assert missing in [item.value for item in outcome.missing_core_elements]
    assert outcome.blocking_questions
    assert outcome.provider_invocations == 0


def test_dataset_specification_compiler_is_deterministic_source_neutral_and_provenanced() -> None:
    endpoint_name = "Thyroid hormone receptor antagonist"
    goal = (
        "Construct a compound-level public training dataset containing chemical structures "
        "and compound-induced transcriptomic responses."
    )
    hints = derive_endpoint_request_semantic_hints(endpoint_name, goal)
    compiler = DatasetSpecificationCompiler()
    kwargs = {
        "endpoint_name": endpoint_name,
        "biological_goal": goal,
        "semantic_hints": hints,
        "target_training_dataset_contract": {"required_fields": MANDATORY_FIELDS},
        "approved_platform_policies": ["source-neutral specification"],
    }
    first = compiler.compile(**kwargs)
    second = compiler.compile(**kwargs)
    assert first == second
    assert first.deterministic_hash == second.deterministic_hash
    assert first.specification is not None
    draft = first.specification
    assert draft.explicit_prediction_grain == "compound × transcriptomic experimental context"
    assert set(DatasetSpecificationCompiler._mandatory_fields) <= set(
        draft.mandatory_target_table_fields
    )
    assert {item.field_name for item in draft.field_provenance} == set(draft.__class__.model_fields)
    serialized = first.model_dump_json().casefold()
    for prohibited in (
        "geo",
        "pubchem",
        "pubmed",
        "tox21",
        "toxcast",
        "lincs",
        "gse",
        "doi",
    ):
        assert prohibited not in serialized


def broad_tr_scope(**changes) -> EndpointDiscoveryScope:
    values = {
        "mode": EndpointDiscoveryMode.BROAD_MODALITY_EXPLORATION,
        "biological_target": "Thyroid hormone receptor",
        "fixed_modality": None,
        "candidate_modalities": [
            EndpointSemanticModality.BINDING,
            EndpointSemanticModality.AGONISM,
            EndpointSemanticModality.ANTAGONISM,
        ],
        "explicitly_excluded_modalities": [],
        "preserve_modalities_separately": True,
        "aggregation_allowed_later": True,
        "aggregation_requires_human_approval": True,
        "selection_deferred_until": "assembly_strategy_review",
        "scientific_scope": (
            "Explore binding, agonism, and antagonism separately and defer endpoint selection."
        ),
        "provenance": [EndpointDiscoveryScopeProvenance.HUMAN_SCOPED_CONFIGURATION],
    }
    values.update(changes)
    return EndpointDiscoveryScope(**values)


def test_fixed_antagonist_compiles_to_fixed_discovery_scope() -> None:
    endpoint = "Thyroid hormone receptor antagonist"
    goal = "Construct a compound-level transcriptomic training dataset."
    outcome = DatasetSpecificationCompiler().compile(
        endpoint_name=endpoint,
        biological_goal=goal,
        semantic_hints=derive_endpoint_request_semantic_hints(endpoint, goal),
        target_training_dataset_contract={},
        approved_platform_policies=[],
    )
    assert outcome.specification is not None
    scope = outcome.endpoint_discovery_scope
    assert scope is not None
    assert scope.mode is EndpointDiscoveryMode.FIXED_MODALITY
    assert scope.fixed_modality is EndpointSemanticModality.ANTAGONISM
    assert scope.candidate_modalities == [EndpointSemanticModality.ANTAGONISM]


def test_human_scoped_broad_tr_compiles_without_automatic_aggregation() -> None:
    endpoint = "Thyroid hormone receptor activity"
    goal = (
        "Determine which public compound-level thyroid hormone receptor activity modalities can "
        "be connected to public compound-induced transcriptomic responses to construct training "
        "datasets."
    )
    scope = broad_tr_scope()
    outcome = DatasetSpecificationCompiler().compile(
        endpoint_name=endpoint,
        biological_goal=goal,
        semantic_hints=derive_endpoint_request_semantic_hints(endpoint, goal),
        target_training_dataset_contract={},
        approved_platform_policies=[],
        discovery_scope=scope,
    )
    assert outcome.status == "compiled"
    assert outcome.provider_invocations == 0
    assert outcome.specification is not None
    draft = outcome.specification
    assert draft.endpoint_modality is None
    assert draft.candidate_modalities == scope.candidate_modalities
    assert outcome.endpoint_discovery_scope == scope
    assert scope.aggregation_active_during_discovery is False
    assert scope.aggregation_allowed_later is True
    assert scope.aggregation_requires_human_approval is True
    assert "compound x assay x modality" in (draft.explicit_prediction_grain or "")
    assert "source_provenance" in draft.mandatory_target_table_fields

    specification = materialize_training_dataset_specification(
        draft,
        specification_id="spec-broad-thyroid-receptor",
        endpoint_discovery_scope=scope,
    )
    requirements = derive_component_requirements(specification)
    activity = next(
        item for item in requirements.requirements if item.requirement_id == "endpoint-activity"
    )
    assert {
        "binding assay evidence",
        "agonism assay evidence",
        "antagonism assay evidence",
    }.issubset(activity.acceptable_data_forms)
    preservation = next(
        item
        for item in requirements.requirements
        if item.requirement_id == "modality-specific-evidence-preservation"
    )
    assert preservation.mandatory is True
    assert "no modality aggregation during discovery" in preservation.quality_requirements


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_modalities": [EndpointSemanticModality.BINDING]},
        {"fixed_modality": EndpointSemanticModality.BINDING},
        {"preserve_modalities_separately": False},
        {"selection_deferred_until": None},
        {"aggregation_requires_human_approval": False},
        {"scientific_scope": "Use a PubChem assay ID as the expected winning modality."},
    ],
)
def test_broad_scope_rejects_invalid_or_source_hint_configuration(changes: dict) -> None:
    with pytest.raises(ValidationError):
        broad_tr_scope(**changes)


def test_compiler_keeps_construction_policies_as_approval_questions() -> None:
    hints = derive_endpoint_request_semantic_hints(
        "X receptor antagonist",
        "Construct a compound-level transcriptomic training dataset.",
    )
    outcome = DatasetSpecificationCompiler().compile(
        endpoint_name="X receptor antagonist",
        biological_goal="Construct a compound-level transcriptomic training dataset.",
        semantic_hints=hints,
        target_training_dataset_contract={},
        approved_platform_policies=[],
    )
    questions = " ".join(outcome.approval_questions).casefold()
    assert "continuous" in questions and "classes" in questions
    assert "observation grain" in questions
    assert "evidence hierarchy" in questions
    assert "aggregation" in questions
    assert "minimum usable coverage" in questions
    assert outcome.blocking_questions == []


def test_compact_reviewer_contract_accepts_clean_and_suggested_correction_outcomes() -> None:
    clean = DatasetSpecificationReviewOutcome(
        schema_version="1.0.0",
        status="no_changes_suggested",
        review_summary="The source-neutral draft is internally consistent.",
        blocking_findings=[],
        approval_questions_to_add=[],
        suggested_field_corrections=[],
        scientific_consistency_flags=[],
        requires_human_review=True,
    )
    corrected = DatasetSpecificationReviewOutcome(
        schema_version="1.0.0",
        status="review_completed",
        review_summary="One clarification is suggested for human consideration.",
        blocking_findings=[],
        approval_questions_to_add=["Should the stated scope be narrowed?"],
        suggested_field_corrections=[
            DatasetSpecificationSuggestedCorrection(
                field_name="intended_scope_of_claim",
                reason="Clarify the requested modality boundary.",
                suggested_value="Limit the claim to the explicitly requested modality.",
            )
        ],
        scientific_consistency_flags=["scope requires human confirmation"],
        requires_human_review=True,
    )
    assert clean.status == "no_changes_suggested"
    assert corrected.suggested_field_corrections[0].field_name == "intended_scope_of_claim"


def test_compact_reviewer_contract_requires_a_finding_for_blocking_status() -> None:
    with pytest.raises(ValidationError, match="at least one finding"):
        DatasetSpecificationReviewOutcome(
            schema_version="1.0.0",
            status="blocking_issue_found",
            review_summary="A blocking issue was claimed without evidence.",
            blocking_findings=[],
            approval_questions_to_add=[],
            suggested_field_corrections=[],
            scientific_consistency_flags=[],
            requires_human_review=True,
        )
