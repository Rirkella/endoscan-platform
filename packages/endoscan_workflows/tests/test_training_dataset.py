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
    DatasetSpecificationCompiler,
    DatasetSpecificationReviewOutcome,
    DatasetSpecificationSuggestedCorrection,
    DiscoveryBeforeStrategyGuard,
    EndpointSemanticModality,
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


def test_semantic_hints_detect_contradictory_modalities() -> None:
    hints = derive_endpoint_request_semantic_hints(
        "X receptor antagonist and agonist",
        "Construct a compound-level prediction training dataset.",
    )
    assert hints.core_definition_status == "contradictory"
    assert "contradictory_core_definition" in hints.missing_core_elements


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
       €ç<∂âûÀk∫wµÁ}ÖëlâπΩëïÃâul¡ulâÕΩ’…çï}•êâtÄÙÄâ•πŸïπ—ïêµÕΩ’…çîà(ÄÄÄÅ•πŸïπ—ïêÄÙÅQ…Ö•π•πùÖ—ÖÕï—ÕÕïµâ±Â…Ö¡†πµΩëï±}ŸÖ±•ëÖ—î°¡ÖÂ±ΩÖê§(ÄÄÄÅ›•—†Å¡Â—ïÕ–π…Ö•ÕïÃ°YÖ±’ï……Ω»∞ÅµÖ—ç†Ùâ’πë•ÕçΩŸï…ïêà§Ë(ÄÄÄÄÄÄÄÅ•πŸïπ—ïêπŸÖ±•ëÖ—ï}•πŸïπ—Ω…‰°•πÿ§(()ëïòÅ—ïÕ—}©Ω•πÖâ•±•—Â}ï·Öç—}¡Ö…—•Ö±}Öπë}…ï≈’•…ïÕ}ëΩ›π±ΩÖë}Ö…ï}ë•Õ—•πç–†§Ä¥¯Å9ΩπîË(ÄÄÄÅï·Öç–ÄÙÅ)Ω•πÖâ•±•—Â•ÖùπΩÕ—•å†(ÄÄÄÄÄÄÄÅë•ÖùπΩÕ—•ç}•êÙâ©Ω•∏µï·Öç–à∞(ÄÄÄÄÄÄÄÅÕ—Ö—’Ãı)Ω•πÖâ•±•—ÂM—Ö—’Ãπ=5AUQ}aP∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•ëÃılâÑà∞Äâàât∞(ÄÄÄÄÄÄÄÅï·Öç—}ΩŸï…±Ö¡}çΩ’π–Ùƒ»∞(ÄÄÄÄ§(ÄÄÄÅ¡Ö…—•Ö∞ÄÙÅ)Ω•πÖâ•±•—Â•ÖùπΩÕ—•å†(ÄÄÄÄÄÄÄÅë•ÖùπΩÕ—•ç}•êÙâ©Ω•∏µ¡Ö…—•Ö∞à∞(ÄÄÄÄÄÄÄÅÕ—Ö—’Ãı)Ω•πÖâ•±•—ÂM—Ö—’Ãπ=5AUQ}AIQ%0∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•ëÃılâÑà∞Äâàât∞(ÄÄÄÄÄÄÄÅ¡Ö…—•Ö±}ΩŸï…±Ö¡}çΩ’π–Ù–∞(ÄÄÄÄ§(ÄÄÄÅëΩ›π±ΩÖêÄÙÅ)Ω•πÖâ•±•—Â•ÖùπΩÕ—•å†(ÄÄÄÄÄÄÄÅë•ÖùπΩÕ—•ç}•êÙâ©Ω•∏µëΩ›π±ΩÖêà∞(ÄÄÄÄÄÄÄÅÕ—Ö—’Ãı)Ω•πÖâ•±•—ÂM—Ö—’ÃπIEU%IM}=]91=∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•ëÃılâÑà∞Äâàât∞(ÄÄÄÄÄÄÄÅ…ï≈’•…ïë}ëΩ›π±ΩÖëÃılâÖç—•Ÿ•—‰Å…ïÕ’±–Å—Öâ±îât∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–Åï·Öç–πï·Öç—}ΩŸï…±Ö¡}çΩ’π–ÄÙÙÄƒ»(ÄÄÄÅÖÕÕï…–Å¡Ö…—•Ö∞πï·Öç—}ΩŸï…±Ö¡}çΩ’π–Å•ÃÅ9Ωπî(ÄÄÄÅÖÕÕï…–ÅëΩ›π±ΩÖêπ…ï≈’•…ïë}ëΩ›π±ΩÖëÃ(()ëïòÅ—ïÕ—}µï—ÖëÖ—Ö}Ωπ±Â}ΩŸï…±Ö¡}çÖππΩ—}ç±Ö•µ}Öπ}ï·Öç—}çΩ’π–†§Ä¥¯Å9ΩπîË(ÄÄÄÅ›•—†Å¡Â—ïÕ–π…Ö•ÕïÃ°YÖ±•ëÖ—•Ωπ……Ω»∞ÅµÖ—ç†Ùâï·Öç–ÅΩŸï…±Ö¿à§Ë(ÄÄÄÄÄÄÄÅ)Ω•πÖâ•±•—Â•ÖùπΩÕ—•å†(ÄÄÄÄÄÄÄÄÄÄÄÅë•ÖùπΩÕ—•ç}•êÙâ©Ω•∏µµï—ÖëÖ—Ñà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’Ãı)Ω•πÖâ•±•—ÂM—Ö—’Ãπ5QQ}=91d∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çï}•ëÃılâÑà∞Äâàât∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·Öç—}ΩŸï…±Ö¡}çΩ’π–Ùƒ¿¿∞(ÄÄÄÄÄÄÄÄ§(()ëïòÅ—ïÕ—}ùÖ¡}ë•…ïç—ïë}ë•ÕçΩŸï…Â}•Õ}âΩ’πëïë}Öπë}ëïë’¡±•çÖ—ïê†§Ä¥¯Å9ΩπîË(ÄÄÄÅ…ï¡Ω…–ÄÙÅÕÕïµâ±ÂÖ¡Iï¡Ω…–†(ÄÄÄÄÄÄÄÅ…ï¡Ω…—}•êÙâùÖ¡Ãµÿƒà∞(ÄÄÄÄÄÄÄÅ•πŸïπ—Ω…Â}•êÙâ•πŸïπ—Ω…‰µùïπï…Ö∞à∞(ÄÄÄÄÄÄÄÅ•πŸïπ—Ω…Â}Ÿï…Õ•Ω∏Ùƒ∞(ÄÄÄÄÄÄÄÅë•ÕçΩŸï…Â}…Ω’πêÙ¿∞(ÄÄÄÄÄÄÄÅµÖ·•µ’µ}ë•ÕçΩŸï…Â}…Ω’πëÃÙ»∞(ÄÄÄÄÄÄÄÅùÖ¡Ãıl(ÄÄÄÄÄÄÄÄÄÄÄÅÕÕïµâ±ÂÖ¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅùÖ¡}•êÙâµ•ÕÕ•πúµ•ëïπ—•—‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩµ¡Ωπïπ–ıΩµ¡Ωπïπ—IΩ±îπM=UI}%}5AA%9∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëïÕç…•¡—•Ω∏Ùâ9ºÅëï—ï…µ•π•Õ—•åÅçΩµ¡Ω’πêÅ•ëïπ—•—‰Åâ…•ëùîÅ•ÃÅÖŸÖ•±Öâ±î∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ±Ωç≠•πúıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—ïë}ÕïÖ…ç°}…ï≈’ïÕ–Ùâô•πêÅΩôô•ç•Ö∞ÅçΩµ¡Ω’πêÅ•ëïπ—•—‰ÅµÖ¡¡•πúà∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕÕïµâ±ÂÖ¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅùÖ¡}•êÙâµ•ÕÕ•πúµ—…ÖπÕç…•¡—Ωµ•çÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩµ¡Ωπïπ–ıΩµ¡Ωπïπ—IΩ±îπQI9MI%AQ=5%}5QI%`∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëïÕç…•¡—•Ω∏Ùâëë•—•ΩπÖ∞Åç°ïµ•çÖ∞Å¡ï…—’…âÖ—•Ω∏Å…ïÕΩ’…çîÅ•ÃÅ…ï≈’•…ïê∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ±Ωç≠•πúıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—ïë}ÕïÖ…ç°}…ï≈’ïÕ–Ùâô•πêÅâ…ΩÖêÅç°ïµ•çÖ∞Å¡ï…—’…âÖ—•Ω∏Å—…ÖπÕç…•¡—Ωµ•çÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅt∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–Å…ï¡Ω…–ππï·—}≈’ï…•ïÃ°Ïâô•πêÅΩôô•ç•Ö∞ÅçΩµ¡Ω’πêÅ•ëïπ—•—‰ÅµÖ¡¡•πúâÙ§ÄÙÙÅl(ÄÄÄÄÄÄÄÄâô•πêÅâ…ΩÖêÅç°ïµ•çÖ∞Å¡ï…—’…âÖ—•Ω∏Å—…ÖπÕç…•¡—Ωµ•çÃà(ÄÄÄÅt(ÄÄÄÅï·°Ö’Õ—ïêÄÙÅ…ï¡Ω…–πµΩëï±}çΩ¡‰°’¡ëÖ—îıÏâë•ÕçΩŸï…Â}…Ω’πêàËÄ…Ù§(ÄÄÄÅÖÕÕï…–Åï·°Ö’Õ—ïêππï·—}≈’ï…•ïÃ°Õï–†§§ÄÙÙÅmt(()ëïòÅ—ïÕ—}¡…ï¡Ö…Ö—•Ωπ}¡±Öπ}…ï≈’•…ïÕ}çΩπ—•ù’Ω’Õ}Ω…ëï»†§Ä¥¯Å9ΩπîË(ÄÄÄÅ›•—†Å¡Â—ïÕ–π…Ö•ÕïÃ°YÖ±•ëÖ—•Ωπ……Ω»∞ÅµÖ—ç†ÙâçΩπ—•ù’Ω’Ãà§Ë(ÄÄÄÄÄÄÄÅQ…Ö•π•πùÖ—ÖÕï—A…ï¡Ö…Ö—•ΩπA±Ö∏†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öπ}•êÙâ¡±Ö∏µ•πŸÖ±•êà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—…Ö—ïùÂ}•êÙâÕ—…Ö—ïù‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—ï¡Ãıl(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅQ…Ö•π•πùÖ—ÖÕï—A…ï¡Ö…Ö—•ΩπM—ï¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—ï¡}•êÙâΩπîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ…ëï»Ùƒ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ω∏ÙâIï—…•ïŸîÅÖç—•Ÿ•—‰Åµï—ÖëÖ—Ñà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃıA…ï¡Ö…Ö—•ΩπM—ï¡M—Ö—’ÃπQI5%9%MQ%}Id∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅQ…Ö•π•πùÖ—ÖÕï—A…ï¡Ö…Ö—•ΩπM—ï¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—ï¡}•êÙâ—°…ïîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ…ëï»ÙÃ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ω∏Ùâ)Ω•∏ÅÕΩ’…çîÅ…ïçΩ…ëÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃıA…ï¡Ö…Ö—•ΩπM—ï¡M—Ö—’ÃπIEU%IM}=5AUQQ%=8∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅt∞(ÄÄÄÄÄÄÄÄ§(()ëïòÅ—ïÕ—}Õ—…Ö—ïùÂ}â•πëÕ}•πŸïπ—Ω…Â}ù…Ö¡°}©Ω•πÖâ•±•—Â}Öπë}¡±Ö∏†§Ä¥¯Å9ΩπîË(ÄÄÄÅ•πÿÄÙÅ•πŸïπ—Ω…‰°ÕΩ’…çî†âÖç—•Ÿ•—‰à∞ÅmΩµ¡Ωπïπ—IΩ±îπ9A=%9Q}Q%Y%Qet§§(ÄÄÄÅù…Ö¡†ÄÙÅô•πÖ±}ù…Ö¡†°•πÿ∞ÅlâÖç—•Ÿ•—‰ât§(ÄÄÄÅë•ÖùπΩÕ—•åÄÙÅ)Ω•πÖâ•±•—Â•ÖùπΩÕ—•å†(ÄÄÄÄÄÄÄÅë•ÖùπΩÕ—•ç}•êÙâ©Ω•∏µëΩ›π±ΩÖêà∞(ÄÄÄÄÄÄÄÅÕ—Ö—’Ãı)Ω•πÖâ•±•—ÂM—Ö—’ÃπIEU%IM}=]91=∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•ëÃılâÖç—•Ÿ•—‰ât∞(ÄÄÄÄÄÄÄÅç±ÖÕÕ}çΩ’π—ÃıÏâÖç—•ŸîàËÄÕÙ∞(ÄÄÄÄÄÄÄÅ…ï≈’•…ïë}ëΩ›π±ΩÖëÃılâ…ïÕ’±–Å—Öâ±îât∞(ÄÄÄÄ§(ÄÄÄÅ¡±Ö∏ÄÙÅQ…Ö•π•πùÖ—ÖÕï—A…ï¡Ö…Ö—•ΩπA±Ö∏†(ÄÄÄÄÄÄÄÅ¡±Öπ}•êÙâ¡±Ö∏µÕΩ’…çîµπï’—…Ö∞à∞(ÄÄÄÄÄÄÄÅÕ—…Ö—ïùÂ}•êÙâÕ—…Ö—ïù‰µÕΩ’…çîµπï’—…Ö∞à∞(ÄÄÄÄÄÄÄÅÕ—ï¡Ãıl(ÄÄÄÄÄÄÄÄÄÄÄÅQ…Ö•π•πùÖ—ÖÕï—A…ï¡Ö…Ö—•ΩπM—ï¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—ï¡}•êÙâ…ï—…•ïŸîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ…ëï»Ùƒ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ω∏ÙâIï—…•ïŸîÅ—°îÅ…ïŸ•ï›ïêÅ…ïÕ’±–Å—Öâ±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃıA…ï¡Ö…Ö—•ΩπM—ï¡M—Ö—’ÃπIEU%IM}=]91=∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅt∞(ÄÄÄÄ§(ÄÄÄÅÕ—…Ö—ïù‰ÄÙÅQ…Ö•π•πùÖ—ÖÕï—ÕÕïµâ±ÂM—…Ö—ïù‰†(ÄÄÄÄÄÄÄÅÕ—…Ö—ïùÂ}•êÙâÕ—…Ö—ïù‰µÕΩ’…çîµπï’—…Ö∞à∞(ÄÄÄÄÄÄÄÅ—Ö…ùï—}Õ¡ïç•ô•çÖ—•Ωπ}•êı•πÿπÕ¡ïç•ô•çÖ—•Ωπ}•ê∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•πŸïπ—Ω…Â}•êı•πÿπ•πŸïπ—Ω…Â}•ê∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}•πŸïπ—Ω…Â}Ÿï…Õ•Ω∏ı•πÿπŸï…Õ•Ω∏∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}ù…Ö¡†ıù…Ö¡†∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}…Ω±ïÃıÏâÖç—•Ÿ•—‰àËÅmΩµ¡Ωπïπ—IΩ±îπ9A=%9Q}Q%Y%QeuÙ∞(ÄÄÄÄÄÄÄÅ•ëïπ—•—Â}¡Ω±•ç‰ÙâUÕîÅëï—ï…µ•π•Õ—•åÅçÖπΩπ•çÖ∞Å•ëïπ—•ô•ï…ÃÅÖπêÅ…ï—Ö•∏ÅçΩπô±•ç—Ã∏à∞(ÄÄÄÄÄÄÄÅç°ïµ•çÖ±}Õ—ÖπëÖ…ë•ÈÖ—•Ωπ}¡Ω±•ç‰Ùâ±ÖúÅµ•·—’…ïÃ∞ÅπΩ…µÖ±•ÈîÅÕÖ±—Ã∞ÅÖπêÅ¡…ïÕï…ŸîÅ¡…ΩŸïπÖπçî∏à∞(ÄÄÄÄÄÄÄÅ±Öâï±}¡Ω±•ç‰ÙâºÅπΩ–Åç…ïÖ—îÅ±Öâï±ÃÅ’π—•∞Å…ïŸ•ï›ïêÅÖç—•Ÿ•—‰Å…ïçΩ…ëÃÅÖ…îÅ•πùïÕ—ïê∏à∞(ÄÄÄÄÄÄÄÅ—…ÖπÕç…•¡—Ωµ•ç}çΩπë•—•Ωπ}¡Ω±•ç‰Ùâ-ïï¿Åçï±∞∞ÅëΩÕîÅÖπêÅ—•µîÅçΩπ—ï·—ÃÅÕï¡Ö…Ö—î∏à∞(ÄÄÄÄÄÄÄÅ…ï¡ïÖ—ïë}Õ•ùπÖ—’…ï}¡Ω±•ç‰ÙâIï—Ö•∏ÅçΩπë•—•Ω∏Å¡…ΩŸïπÖπçîÅâïôΩ…îÅÖπ‰ÅÖùù…ïùÖ—•Ω∏∏à∞(ÄÄÄÄÄÄÄÅï·¡ïç—ïë}Ω’—¡’—}ù…Ö•∏ÙâΩπîÅ…Ω‹Å¡ï»ÅçΩµ¡Ω’πêÅÖπêÅçΩπ—ï·–à∞(ÄÄÄÄÄÄÄÅΩŸï…±Ö¡}ë•ÖùπΩÕ—•åıë•ÖùπΩÕ—•å∞(ÄÄÄÄÄÄÄÅï·¡ïç—ïë}çΩŸï…ÖùîıÏâÖç—•Ÿ•—‰àËÄ¿∏·Ù∞(ÄÄÄÄÄÄÄÅï·¡ïç—ïë}ç±ÖÕÕ}âÖ±ÖπçîıÏâÖç—•ŸîàËÄÕÙ∞(ÄÄÄÄÄÄÄÅïŸ•ëïπçï}≈’Ö±•—‰Ùâ=ôô•ç•Ö∞Åµï—ÖëÖ—ÑÅΩπ±‰ÏÅÕΩ’…çîÅ•πùïÕ—•Ω∏Å•ÃÅÕ—•±∞Å…ï≈’•…ïê∏à∞(ÄÄÄÄÄÄÄÅ¡…ï¡Ö…Ö—•Ωπ}ïôôΩ…–Ùâ…ï≈’•…ïÃÅâΩ’πëïêÅÕΩ’…çîÅ•πùïÕ—•Ω∏à∞(ÄÄÄÄÄÄÄÅµ•ÕÕ•πù}çΩµ¡Ωπïπ—ÃımΩµ¡Ωπïπ—IΩ±îπQI9MI%AQ=5%}5QI%at∞(ÄÄÄÄÄÄÄÅ¡…ï¡Ö…Ö—•Ωπ}¡±Ö∏ı¡±Ö∏∞(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃıM—…Ö—ïùÂM—Ö—’ÃπIEU%IM}%Q%=91}%M=YId∞(ÄÄÄÄ§(ÄÄÄÅÕ—…Ö—ïù‰πŸÖ±•ëÖ—ï}•πŸïπ—Ω…‰°•πÿ§(ÄÄÄÅÖÕÕï…–ÅÕ—…Ö—ïù‰πÕΩ’…çï}…Ω±ïÕl¡tπÕΩ’…çï}•êÄÙÙÄâÖç—•Ÿ•—‰à(ÄÄÄÅÖÕÕï…–ÅÕ—…Ö—ïù‰πï·¡ïç—ïë}çΩŸï…Öùïl¡tππÖµîÄÙÙÄâÖç—•Ÿ•—‰à(ÄÄÄÅÖÕÕï…–ÅÕ—…Ö—ïù‰πï·¡ïç—ïë}ç±ÖÕÕ}âÖ±Öπçïl¡tππÖµîÄÙÙÄâÖç—•Ÿîà(ÄÄÄÅÖÕÕï…–ÅÕ—…Ö—ïù‰πΩŸï…±Ö¡}ë•ÖùπΩÕ—•åπç±ÖÕÕ}çΩ’π—Õl¡tπ±Öâï∞ÄÙÙÄâÖç—•Ÿîà(()ëïòÅ—ïÕ—}â±•πë}çΩπ—ï·—}çΩπ—Ö•πÕ}πΩ}ïπë¡Ω•π—}Õ¡ïç•ô•ç}°•π—Ã†§Ä¥¯Å9ΩπîË(ÄÄÄÅçΩπ—ï·–ÄÙÅ	±•πë	ïπç°µÖ…≠%π•—•Ö±Ωπ—ï·–†(ÄÄÄÄÄÄÄÅâïπç°µÖ…≠}µΩëîı	1%9}QI%9%9}QMQ}%M=YId∞(ÄÄÄÄÄÄÄÅïπë¡Ω•π—}πÖµîÙâ·Öµ¡±îÅïπë¡Ω•π–à∞(ÄÄÄÄÄÄÄÅâ•Ω±Ωù•çÖ±}ùΩÖ∞ÙâΩπÕ—…’ç–ÅÑÅ¡’â±•åµëÖ—ÑÅ—…Ö•π•πúÅëÖ—ÖÕï–Å›•—°Ω’–ÅÕΩ’…çîÅ°•π—Ã∏à∞(ÄÄÄÄÄÄÄÅ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–ıÏâ…ï≈’•…ïë}ô•ï±ëÃàËÅ59Q=Ie}%1MÙ∞(ÄÄÄÄÄÄÄÅÕΩ’…çï}ÖëÖ¡—ï…}çÖ¡Öâ•±•—•ïÃılâΩôô•ç•Ö∞ÅÕ—…’ç—’…ïêÅÕΩ’…çîÅÖëÖ¡—ï…Ãât∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸïë}Õç•ïπ—•ô•ç}¡Ω±•ç•ïÃılâë•ÕçΩŸï…‰ÅâïôΩ…îÅÕ—…Ö—ïù‰ât∞(ÄÄÄÄÄÄÄÅÖ±±Ω›ïë}—ΩΩ±ÃılâÕïÖ…ç°}Öç—•Ÿ•—Â}ÕΩ’…çïÃât∞(ÄÄÄÄÄÄÄÅ¡±Öππï…}¡…ΩŸ•ëï»ÙâôÖ≠îà∞(ÄÄÄÄÄÄÄÅ¡±Öππï…}µΩëï∞Ùâ¡±Öππï»µô•·—’…îà∞(ÄÄÄÄÄÄÄÅ›Ω…≠ï…}¡…ΩŸ•ëï»ÙâôÖ≠îà∞(ÄÄÄÄÄÄÄÅ›Ω…≠ï…}µΩëï∞Ùâ›Ω…≠ï»µô•·—’…îà∞(ÄÄÄÄÄÄÄÅâ’ëùï—ÃıÏâ¡…ΩŸ•ëï…}…ï—…•ïÃàËÄ¡Ù∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–ÅçΩπ—ï·–πÕΩ’…çï}°•π—ÃÄÙÙÅmt(ÄÄÄÅÖÕÕï…–ÅçΩπ—ï·–πÖ…—•ç±ï}°•π–Å•ÃÅ9Ωπî(ÄÄÄÅÖÕÕï…–ÅçΩπ—ï·–πÖÕÕÖÂ}•ë}°•π–Å•ÃÅ9Ωπî(()ëïòÅ—ïÕ—}â±•πë}çΩπ—ï·—}…ï©ïç—Õ}ÕΩ’…çï}°•π—Ã†§Ä¥¯Å9ΩπîË(ÄÄÄÅ›•—†Å¡Â—ïÕ–π…Ö•ÕïÃ°YÖ±•ëÖ—•Ωπ……Ω»§Ë(ÄÄÄÄÄÄÄÅ	±•πë	ïπç°µÖ…≠%π•—•Ö±Ωπ—ï·–†(ÄÄÄÄÄÄÄÄÄÄÄÅâïπç°µÖ…≠}µΩëîı	1%9}QI%9%9}QMQ}%M=YId∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπë¡Ω•π—}πÖµîÙâ·Öµ¡±îÅïπë¡Ω•π–à∞(ÄÄÄÄÄÄÄÄÄÄÄÅâ•Ω±Ωù•çÖ±}ùΩÖ∞ÙâΩπÕ—…’ç–ÅÑÅ¡’â±•åµëÖ—ÑÅ—…Ö•π•πúÅëÖ—ÖÕï–Å›•—°Ω’–ÅÕΩ’…çîÅ°•π—Ã∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–ıÏâ…ï≈’•…ïë}ô•ï±ëÃàËÅ59Q=Ie}%1MÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öππï…}¡…ΩŸ•ëï»ÙâôÖ≠îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öππï…}µΩëï∞Ùâ¡±Öππï»µô•·—’…îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ›Ω…≠ï…}¡…ΩŸ•ëï»ÙâôÖ≠îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ›Ω…≠ï…}µΩëï∞Ùâ›Ω…≠ï»µô•·—’…îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅâ’ëùï—ÃıÏâ¡…ΩŸ•ëï…}…ï—…•ïÃàËÄ¡Ù∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çï}°•π—Ãılâ≠πΩ›∏µÕΩ’…çîât∞(ÄÄÄÄÄÄÄÄ§(()¡Â—ïÕ–πµÖ…¨π¡Ö…Öµï—…•Èî†(ÄÄÄÄ†âïπë¡Ω•π—}πÖµîà∞ÄâùΩÖ∞à∞Äâ—Ö…ùï–à∞ÄâµΩëÖ±•—‰à§∞(ÄÄÄÅl(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅÑÅçΩµ¡Ω’πêµ±ïŸï∞Å—…Ö•π•πúÅëÖ—ÖÕï–Å›•—†Å—…ÖπÕç…•¡—Ωµ•åÅ…ïÕ¡ΩπÕïÃ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖπ—ÖùΩπ•Õ¥à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâdÅ…ïçï¡—Ω»ÅÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêÅ—…Ö•π•πúÅëÖ—ÑÅ›•—†Å—…ÖπÕç…•¡—Ωµ•åÅ…ïÕ¡ΩπÕïÃ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâdÅ…ïçï¡—Ω»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖùΩπ•Õ¥à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâhÅïπÈÂµîÅ•π°•â•—Ω»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâhÅïπÈÂµîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ•π°•â•—•Ω∏à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâÅ…ïçï¡—Ω»Åâ•πë•πúà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÅ…ïçï¡—Ω»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄââ•πë•πúà∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÅt∞(§)ëïòÅ—ïÕ—}ëÖ—ÖÕï—}Õ¡ïç•ô•çÖ—•Ωπ}çΩµ¡•±ï…}°Öπë±ïÕ}ï·¡±•ç•—}çΩ…ï}…ï≈’ïÕ—Ã†(ÄÄÄÅïπë¡Ω•π—}πÖµîËÅÕ—»∞(ÄÄÄÅùΩÖ∞ËÅÕ—»∞(ÄÄÄÅ—Ö…ùï–ËÅÕ—»∞(ÄÄÄÅµΩëÖ±•—‰ËÅÕ—»∞(§Ä¥¯Å9ΩπîË(ÄÄÄÅ°•π—ÃÄÙÅëï…•Ÿï}ïπë¡Ω•π—}…ï≈’ïÕ—}ÕïµÖπ—•ç}°•π—Ã°ïπë¡Ω•π—}πÖµî∞ÅùΩÖ∞§(ÄÄÄÅΩ’—çΩµîÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπΩµ¡•±ï»†§πçΩµ¡•±î†(ÄÄÄÄÄÄÄÅïπë¡Ω•π—}πÖµîıïπë¡Ω•π—}πÖµî∞(ÄÄÄÄÄÄÄÅâ•Ω±Ωù•çÖ±}ùΩÖ∞ıùΩÖ∞∞(ÄÄÄÄÄÄÄÅÕïµÖπ—•ç}°•π—Ãı°•π—Ã∞(ÄÄÄÄÄÄÄÅ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–ıÏâ…ï≈’•…ïë}ô•ï±ëÃàËÅ59Q=Ie}%1MÙ∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸïë}¡±Ö—ôΩ…µ}¡Ω±•ç•ïÃılâÕΩ’…çîµπï’—…Ö∞ÅÕ¡ïç•ô•çÖ—•Ω∏ât∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ—Ö—’ÃÄÙÙÄâçΩµ¡•±ïêà(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπ¡…ΩŸ•ëï…}•πŸΩçÖ—•ΩπÃÄÙÙÄ¿(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ¡ïç•ô•çÖ—•Ω∏Å•ÃÅπΩ–Å9Ωπî(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ¡ïç•ô•çÖ—•Ω∏πâ•Ω±Ωù•çÖ±}—Ö…ùï–ÄÙÙÅ—Ö…ùï–(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ¡ïç•ô•çÖ—•Ω∏πïπë¡Ω•π—}µΩëÖ±•—‰ÄÙÙÅµΩëÖ±•—‰(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ¡ïç•ô•çÖ—•Ω∏πâ±Ωç≠•πù}≈’ïÕ—•ΩπÃÄÙÙÅmt(()¡Â—ïÕ–πµÖ…¨π¡Ö…Öµï—…•Èî†(ÄÄÄÄ†âïπë¡Ω•π—}πÖµîà∞ÄâùΩÖ∞à∞Äâµ•ÕÕ•πúà§∞(ÄÄÄÅl(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Ω·•ç•—‰ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄââ•Ω±Ωù•çÖ±}—Ö…ùï—}Ω…}¡…ΩçïÕÃà∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï≈’ïÕ—ïë}µΩëÖ±•—Â}Ω…}¡…ïë•ç—•Ωπ}ç±Ö•¥à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»ÅÖùΩπ•Õ–ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—…Öë•ç—Ω…Â}çΩ…ï}ëïô•π•—•Ω∏à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ·¡±Ω…îÅ¡’â±•åÅëÖ—Ñ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡Ω’πë}±ïŸï±}¡…ïë•ç—•Ωπ}Ωâ©ïç—•Ÿîà∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÅt∞(§)ëïòÅ—ïÕ—}ëÖ—ÖÕï—}Õ¡ïç•ô•çÖ—•Ωπ}çΩµ¡•±ï…}…ï≈’•…ïÕ}ï·Öç—}µ•ÕÕ•πù}çΩ…ï}ô•ï±ëÃ†(ÄÄÄÅïπë¡Ω•π—}πÖµîËÅÕ—»∞(ÄÄÄÅùΩÖ∞ËÅÕ—»∞(ÄÄÄÅµ•ÕÕ•πúËÅÕ—»∞(§Ä¥¯Å9ΩπîË(ÄÄÄÅ°•π—ÃÄÙÅëï…•Ÿï}ïπë¡Ω•π—}…ï≈’ïÕ—}ÕïµÖπ—•ç}°•π—Ã°ïπë¡Ω•π—}πÖµî∞ÅùΩÖ∞§(ÄÄÄÅΩ’—çΩµîÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπΩµ¡•±ï»†§πçΩµ¡•±î†(ÄÄÄÄÄÄÄÅïπë¡Ω•π—}πÖµîıïπë¡Ω•π—}πÖµî∞(ÄÄÄÄÄÄÄÅâ•Ω±Ωù•çÖ±}ùΩÖ∞ıùΩÖ∞∞(ÄÄÄÄÄÄÄÅÕïµÖπ—•ç}°•π—Ãı°•π—Ã∞(ÄÄÄÄÄÄÄÅ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–ıÌÙ∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸïë}¡±Ö—ôΩ…µ}¡Ω±•ç•ïÃımt∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ—Ö—’ÃÄÙÙÄâπïïëÕ}°’µÖπ}ç±Ö…•ô•çÖ—•Ω∏à(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπÕ¡ïç•ô•çÖ—•Ω∏Å•ÃÅ9Ωπî(ÄÄÄÅÖÕÕï…–Åµ•ÕÕ•πúÅ•∏Åm•—ï¥πŸÖ±’îÅôΩ»Å•—ï¥Å•∏ÅΩ’—çΩµîπµ•ÕÕ•πù}çΩ…ï}ï±ïµïπ—Õt(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπâ±Ωç≠•πù}≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπ¡…ΩŸ•ëï…}•πŸΩçÖ—•ΩπÃÄÙÙÄ¿(()ëïòÅ—ïÕ—}ëÖ—ÖÕï—}Õ¡ïç•ô•çÖ—•Ωπ}çΩµ¡•±ï…}•Õ}ëï—ï…µ•π•Õ—•ç}ÕΩ’…çï}πï’—…Ö±}Öπë}¡…ΩŸïπÖπçïê†§Ä¥¯Å9ΩπîË(ÄÄÄÅïπë¡Ω•π—}πÖµîÄÙÄâQ°Â…Ω•êÅ°Ω…µΩπîÅ…ïçï¡—Ω»ÅÖπ—ÖùΩπ•Õ–à(ÄÄÄÅùΩÖ∞ÄÙÄ†(ÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅÑÅçΩµ¡Ω’πêµ±ïŸï∞Å¡’â±•åÅ—…Ö•π•πúÅëÖ—ÖÕï–ÅçΩπ—Ö•π•πúÅç°ïµ•çÖ∞ÅÕ—…’ç—’…ïÃÄà(ÄÄÄÄÄÄÄÄâÖπêÅçΩµ¡Ω’πêµ•πë’çïêÅ—…ÖπÕç…•¡—Ωµ•åÅ…ïÕ¡ΩπÕïÃ∏à(ÄÄÄÄ§(ÄÄÄÅ°•π—ÃÄÙÅëï…•Ÿï}ïπë¡Ω•π—}…ï≈’ïÕ—}ÕïµÖπ—•ç}°•π—Ã°ïπë¡Ω•π—}πÖµî∞ÅùΩÖ∞§(ÄÄÄÅçΩµ¡•±ï»ÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπΩµ¡•±ï»†§(ÄÄÄÅ≠›Ö…ùÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâïπë¡Ω•π—}πÖµîàËÅïπë¡Ω•π—}πÖµî∞(ÄÄÄÄÄÄÄÄââ•Ω±Ωù•çÖ±}ùΩÖ∞àËÅùΩÖ∞∞(ÄÄÄÄÄÄÄÄâÕïµÖπ—•ç}°•π—ÃàËÅ°•π—Ã∞(ÄÄÄÄÄÄÄÄâ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–àËÅÏâ…ï≈’•…ïë}ô•ï±ëÃàËÅ59Q=Ie}%1MÙ∞(ÄÄÄÄÄÄÄÄâÖ¡¡…ΩŸïë}¡±Ö—ôΩ…µ}¡Ω±•ç•ïÃàËÅlâÕΩ’…çîµπï’—…Ö∞ÅÕ¡ïç•ô•çÖ—•Ω∏ât∞(ÄÄÄÅÙ(ÄÄÄÅô•…Õ–ÄÙÅçΩµ¡•±ï»πçΩµ¡•±î†®©≠›Ö…ùÃ§(ÄÄÄÅÕïçΩπêÄÙÅçΩµ¡•±ï»πçΩµ¡•±î†®©≠›Ö…ùÃ§(ÄÄÄÅÖÕÕï…–Åô•…Õ–ÄÙÙÅÕïçΩπê(ÄÄÄÅÖÕÕï…–Åô•…Õ–πëï—ï…µ•π•Õ—•ç}°ÖÕ†ÄÙÙÅÕïçΩπêπëï—ï…µ•π•Õ—•ç}°ÖÕ†(ÄÄÄÅÖÕÕï…–Åô•…Õ–πÕ¡ïç•ô•çÖ—•Ω∏Å•ÃÅπΩ–Å9Ωπî(ÄÄÄÅë…Öô–ÄÙÅô•…Õ–πÕ¡ïç•ô•çÖ—•Ω∏(ÄÄÄÅÖÕÕï…–Åë…Öô–πï·¡±•ç•—}¡…ïë•ç—•Ωπ}ù…Ö•∏ÄÙÙÄâçΩµ¡Ω’πêÉ\Å—…ÖπÕç…•¡—Ωµ•åÅï·¡ï…•µïπ—Ö∞ÅçΩπ—ï·–à(ÄÄÄÅÖÕÕï…–ÅÕï–°Ö—ÖÕï—M¡ïç•ô•çÖ—•ΩπΩµ¡•±ï»π}µÖπëÖ—Ω…Â}ô•ï±ëÃ§ÄÙÅÕï–†(ÄÄÄÄÄÄÄÅë…Öô–πµÖπëÖ—Ω…Â}—Ö…ùï—}—Öâ±ï}ô•ï±ëÃ(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–ÅÌ•—ï¥πô•ï±ë}πÖµîÅôΩ»Å•—ï¥Å•∏Åë…Öô–πô•ï±ë}¡…ΩŸïπÖπçïÙÄÙÙÅÕï–°ë…Öô–π}}ç±ÖÕÕ}|πµΩëï±}ô•ï±ëÃ§(ÄÄÄÅÕï…•Ö±•ÈïêÄÙÅô•…Õ–πµΩëï±}ë’µ¡}©ÕΩ∏†§πçÖÕïôΩ±ê†§(ÄÄÄÅôΩ»Å¡…Ω°•â•—ïêÅ•∏Ä†(ÄÄÄÄÄÄÄÄâùïºà∞(ÄÄÄÄÄÄÄÄâ¡’âç°ï¥à∞(ÄÄÄÄÄÄÄÄâ¡’âµïêà∞(ÄÄÄÄÄÄÄÄâ—Ω‡»ƒà∞(ÄÄÄÄÄÄÄÄâ—Ω·çÖÕ–à∞(ÄÄÄÄÄÄÄÄâ±•πçÃà∞(ÄÄÄÄÄÄÄÄâùÕîà∞(ÄÄÄÄÄÄÄÄâëΩ§à∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅÖÕÕï…–Å¡…Ω°•â•—ïêÅπΩ–Å•∏ÅÕï…•Ö±•Èïê(()ëïòÅ—ïÕ—}çΩµ¡•±ï…}≠ïï¡Õ}çΩπÕ—…’ç—•Ωπ}¡Ω±•ç•ïÕ}ÖÕ}Ö¡¡…ΩŸÖ±}≈’ïÕ—•ΩπÃ†§Ä¥¯Å9ΩπîË(ÄÄÄÅ°•π—ÃÄÙÅëï…•Ÿï}ïπë¡Ω•π—}…ï≈’ïÕ—}ÕïµÖπ—•ç}°•π—Ã†(ÄÄÄÄÄÄÄÄâ`Å…ïçï¡—Ω»ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÄâΩπÕ—…’ç–ÅÑÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—ÖÕï–∏à∞(ÄÄÄÄ§(ÄÄÄÅΩ’—çΩµîÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπΩµ¡•±ï»†§πçΩµ¡•±î†(ÄÄÄÄÄÄÄÅïπë¡Ω•π—}πÖµîÙâ`Å…ïçï¡—Ω»ÅÖπ—ÖùΩπ•Õ–à∞(ÄÄÄÄÄÄÄÅâ•Ω±Ωù•çÖ±}ùΩÖ∞ÙâΩπÕ—…’ç–ÅÑÅçΩµ¡Ω’πêµ±ïŸï∞Å—…ÖπÕç…•¡—Ωµ•åÅ—…Ö•π•πúÅëÖ—ÖÕï–∏à∞(ÄÄÄÄÄÄÄÅÕïµÖπ—•ç}°•π—Ãı°•π—Ã∞(ÄÄÄÄÄÄÄÅ—Ö…ùï—}—…Ö•π•πù}ëÖ—ÖÕï—}çΩπ—…Öç–ıÌÙ∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸïë}¡±Ö—ôΩ…µ}¡Ω±•ç•ïÃımt∞(ÄÄÄÄ§(ÄÄÄÅ≈’ïÕ—•ΩπÃÄÙÄàÄàπ©Ω•∏°Ω’—çΩµîπÖ¡¡…ΩŸÖ±}≈’ïÕ—•ΩπÃ§πçÖÕïôΩ±ê†§(ÄÄÄÅÖÕÕï…–ÄâçΩπ—•π’Ω’ÃàÅ•∏Å≈’ïÕ—•ΩπÃÅÖπêÄâç±ÖÕÕïÃàÅ•∏Å≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–ÄâΩâÕï…ŸÖ—•Ω∏Åù…Ö•∏àÅ•∏Å≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–ÄâïŸ•ëïπçîÅ°•ï…Ö…ç°‰àÅ•∏Å≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–ÄâÖùù…ïùÖ—•Ω∏àÅ•∏Å≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–Äâµ•π•µ’¥Å’ÕÖâ±îÅçΩŸï…ÖùîàÅ•∏Å≈’ïÕ—•ΩπÃ(ÄÄÄÅÖÕÕï…–ÅΩ’—çΩµîπâ±Ωç≠•πù}≈’ïÕ—•ΩπÃÄÙÙÅmt(()ëïòÅ—ïÕ—}çΩµ¡Öç—}…ïŸ•ï›ï…}çΩπ—…Öç—}Öççï¡—Õ}ç±ïÖπ}Öπë}Õ’ùùïÕ—ïë}çΩ……ïç—•Ωπ}Ω’—çΩµïÃ†§Ä¥¯Å9ΩπîË(ÄÄÄÅç±ïÖ∏ÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπIïŸ•ï›=’—çΩµî†(ÄÄÄÄÄÄÄÅÕç°ïµÖ}Ÿï…Õ•Ω∏Ùàƒ∏¿∏¿à∞(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÙâπΩ}ç°ÖπùïÕ}Õ’ùùïÕ—ïêà∞(ÄÄÄÄÄÄÄÅ…ïŸ•ï›}Õ’µµÖ…‰ÙâQ°îÅÕΩ’…çîµπï’—…Ö∞Åë…Öô–Å•ÃÅ•π—ï…πÖ±±‰ÅçΩπÕ•Õ—ïπ–∏à∞(ÄÄÄÄÄÄÄÅâ±Ωç≠•πù}ô•πë•πùÃımt∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸÖ±}≈’ïÕ—•ΩπÕ}—Ω}Öëêımt∞(ÄÄÄÄÄÄÄÅÕ’ùùïÕ—ïë}ô•ï±ë}çΩ……ïç—•ΩπÃımt∞(ÄÄÄÄÄÄÄÅÕç•ïπ—•ô•ç}çΩπÕ•Õ—ïπçÂ}ô±ÖùÃımt∞(ÄÄÄÄÄÄÄÅ…ï≈’•…ïÕ}°’µÖπ}…ïŸ•ï‹ıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅçΩ……ïç—ïêÄÙÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπIïŸ•ï›=’—çΩµî†(ÄÄÄÄÄÄÄÅÕç°ïµÖ}Ÿï…Õ•Ω∏Ùàƒ∏¿∏¿à∞(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÙâ…ïŸ•ï›}çΩµ¡±ï—ïêà∞(ÄÄÄÄÄÄÄÅ…ïŸ•ï›}Õ’µµÖ…‰Ùâ=πîÅç±Ö…•ô•çÖ—•Ω∏Å•ÃÅÕ’ùùïÕ—ïêÅôΩ»Å°’µÖ∏ÅçΩπÕ•ëï…Ö—•Ω∏∏à∞(ÄÄÄÄÄÄÄÅâ±Ωç≠•πù}ô•πë•πùÃımt∞(ÄÄÄÄÄÄÄÅÖ¡¡…ΩŸÖ±}≈’ïÕ—•ΩπÕ}—Ω}ÖëêılâM°Ω’±êÅ—°îÅÕ—Ö—ïêÅÕçΩ¡îÅâîÅπÖ……Ω›ïê¸ât∞(ÄÄÄÄÄÄÄÅÕ’ùùïÕ—ïë}ô•ï±ë}çΩ……ïç—•ΩπÃıl(ÄÄÄÄÄÄÄÄÄÄÄÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπM’ùùïÕ—ïëΩ……ïç—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅô•ï±ë}πÖµîÙâ•π—ïπëïë}ÕçΩ¡ï}Ωô}ç±Ö•¥à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÖÕΩ∏Ùâ±Ö…•ô‰Å—°îÅ…ï≈’ïÕ—ïêÅµΩëÖ±•—‰ÅâΩ’πëÖ…‰∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ’ùùïÕ—ïë}ŸÖ±’îÙâ1•µ•–Å—°îÅç±Ö•¥Å—ºÅ—°îÅï·¡±•ç•—±‰Å…ï≈’ïÕ—ïêÅµΩëÖ±•—‰∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅt∞(ÄÄÄÄÄÄÄÅÕç•ïπ—•ô•ç}çΩπÕ•Õ—ïπçÂ}ô±ÖùÃılâÕçΩ¡îÅ…ï≈’•…ïÃÅ°’µÖ∏ÅçΩπô•…µÖ—•Ω∏ât∞(ÄÄÄÄÄÄÄÅ…ï≈’•…ïÕ}°’µÖπ}…ïŸ•ï‹ıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅÖÕÕï…–Åç±ïÖ∏πÕ—Ö—’ÃÄÙÙÄâπΩ}ç°ÖπùïÕ}Õ’ùùïÕ—ïêà(ÄÄÄÅÖÕÕï…–ÅçΩ……ïç—ïêπÕ’ùùïÕ—ïë}ô•ï±ë}çΩ……ïç—•ΩπÕl¡tπô•ï±ë}πÖµîÄÙÙÄâ•π—ïπëïë}ÕçΩ¡ï}Ωô}ç±Ö•¥à(()ëïòÅ—ïÕ—}çΩµ¡Öç—}…ïŸ•ï›ï…}çΩπ—…Öç—}…ï≈’•…ïÕ}Ö}ô•πë•πù}ôΩ…}â±Ωç≠•πù}Õ—Ö—’Ã†§Ä¥¯Å9ΩπîË(ÄÄÄÅ›•—†Å¡Â—ïÕ–π…Ö•ÕïÃ°YÖ±•ëÖ—•Ωπ……Ω»∞ÅµÖ—ç†ÙâÖ–Å±ïÖÕ–ÅΩπîÅô•πë•πúà§Ë(ÄÄÄÄÄÄÄÅÖ—ÖÕï—M¡ïç•ô•çÖ—•ΩπIïŸ•ï›=’—çΩµî†(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïµÖ}Ÿï…Õ•Ω∏Ùàƒ∏¿∏¿à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃÙââ±Ωç≠•πù}•ÕÕ’ï}ôΩ’πêà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïŸ•ï›}Õ’µµÖ…‰ÙâÅâ±Ωç≠•πúÅ•ÕÕ’îÅ›ÖÃÅç±Ö•µïêÅ›•—°Ω’–ÅïŸ•ëïπçî∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅâ±Ωç≠•πù}ô•πë•πùÃımt∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡…ΩŸÖ±}≈’ïÕ—•ΩπÕ}—Ω}Öëêımt∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ’ùùïÕ—ïë}ô•ï±ë}çΩ……ïç—•ΩπÃımt∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕç•ïπ—•ô•ç}çΩπÕ•Õ—ïπçÂ}ô±ÖùÃımt∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’•…ïÕ}°’µÖπ}…ïŸ•ï‹ıQ…’î∞(ÄÄÄÄÄÄÄÄ§