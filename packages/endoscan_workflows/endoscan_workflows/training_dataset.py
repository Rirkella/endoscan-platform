"""Source-neutral contracts for public training-dataset discovery and assembly.

The module deliberately contains no endpoint-specific source hints and performs no
network access.  It is the validated artifact boundary between specialized agents
and the deterministic workflow orchestrator.
"""

from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import StrictContract

TRAINING_DATASET_CONTRACT_VERSION = "1.0.0"
BLIND_TRAINING_DATASET_DISCOVERY = "blind_training_dataset_discovery"


class PredictionUnit(StrEnum):
    COMPOUND = "compound"
    COMPOUND_CONTEXT = "compound_cell_context"
    COMPOUND_CONTEXT_DOSE_TIME = "compound_cell_context_dose_time"
    AGGREGATED_COMPOUND_PROFILE = "aggregated_compound_profile"
    EXPLICIT_OTHER = "explicit_other"


class ActivityRepresentation(StrEnum):
    BINARY = "binary_active_inactive"
    MULTICLASS = "multiclass"
    CONTINUOUS = "continuous_activity"
    POTENCY = "potency"
    EFFICACY = "efficacy"
    DOSE_RESPONSE = "dose_response_summary"
    AGGREGATED_EVIDENCE = "aggregated_evidence_score"


class ComponentRole(StrEnum):
    ENDPOINT_ACTIVITY = "endpoint_activity"
    ASSAY_METADATA = "assay_metadata"
    COUNTER_SCREEN = "counter_screen"
    COMPOUND_IDENTITY = "compound_identity"
    CHEMICAL_STRUCTURE = "chemical_structure"
    TRANSCRIPTOMIC_MATRIX = "transcriptomic_matrix"
    TRANSCRIPTOMIC_CONDITIONS = "transcriptomic_conditions"
    SAMPLE_METADATA = "sample_metadata"
    SOURCE_ID_MAPPING = "source_id_mapping"
    METHODOLOGICAL_EVIDENCE = "methodological_evidence"
    PROVENANCE_LICENSE = "provenance_license"
    VALIDATION_REFERENCE = "validation_reference"


class CapabilityStatus(StrEnum):
    VERIFIED_AVAILABLE = "verified_available"
    PARTIALLY_AVAILABLE = "partially_available"
    METADATA_ONLY = "metadata_only"
    REQUIRES_DOWNLOAD = "requires_download"
    UNAVAILABLE = "unavailable"
    UNRESOLVED = "unresolved"


class SourceValidationStatus(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    METADATA_ONLY = "metadata_only"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


class GraphNodeType(StrEnum):
    SOURCE = "source"
    TRANSFORMATION = "transformation"
    IDENTITY_RESOLUTION = "identity_resolution"
    LABEL_CURATION = "label_curation"
    FILTERING = "filtering"
    AGGREGATION = "aggregation"
    JOIN = "join"
    VALIDATION = "validation"
    APPROVAL = "approval"
    FINAL_CANDIDATE_TABLE = "final_candidate_table"


class StrategyStatus(StrEnum):
    RECOMMENDED_FOR_HUMAN_REVIEW = "recommended_for_human_review"
    ALTERNATIVE_STRATEGY = "alternative_strategy"
    REQUIRES_SOURCE_INGESTION = "requires_source_ingestion"
    REQUIRES_IDENTITY_RESOLUTION = "requires_identity_resolution"
    REQUIRES_LABEL_CURATION = "requires_label_curation"
    REQUIRES_ADDITIONAL_DISCOVERY = "requires_additional_discovery"
    INSUFFICIENT_PUBLIC_EVIDENCE = "insufficient_public_evidence"
    SCIENTIFICALLY_MISALIGNED = "scientifically_misaligned"
    NOT_FEASIBLE = "not_feasible"


class JoinabilityStatus(StrEnum):
    COMPUTED_EXACT = "computed_exact"
    COMPUTED_PARTIAL = "computed_partial"
    METADATA_ONLY = "metadata_only"
    REQUIRES_DOWNLOAD = "requires_download"
    REQUIRES_MANUAL_MAPPING = "requires_manual_mapping"
    NOT_COMPUTABLE = "not_computable"


class PreparationStepStatus(StrEnum):
    COMPLETED = "completed"
    DETERMINISTIC_READY = "deterministic_and_ready"
    REQUIRES_DOWNLOAD = "requires_download"
    REQUIRES_COMPUTATION = "requires_computation"
    REQUIRES_AGENT_REVIEW = "requires_agent_review"
    REQUIRES_HUMAN_APPROVAL = "requires_human_approval"
    BLOCKED = "blocked"


class TrainingDatasetSpecification(StrictContract):
    """Versioned definition of the training table the workflow must construct."""

    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    specification_id: str = Field(min_length=3, max_length=160)
    endpoint_name: str = Field(min_length=3, max_length=160)
    biological_target: str = Field(min_length=1, max_length=500)
    endpoint_modality: str = Field(min_length=1, max_length=300)
    endpoint_definition: str = Field(min_length=10, max_length=4000)
    intended_prediction_task: str = Field(min_length=10, max_length=2000)
    prediction_unit: PredictionUnit
    explicit_prediction_grain: str | None = Field(default=None, max_length=1000)
    acceptable_activity_representations: list[ActivityRepresentation] = Field(
        min_length=1, max_length=10
    )
    acceptable_transcriptomic_representations: list[str] = Field(min_length=1, max_length=30)
    compound_identity_requirements: list[str] = Field(min_length=1, max_length=30)
    chemical_structure_requirements: list[str] = Field(min_length=1, max_length=30)
    experimental_context_requirements: list[str] = Field(min_length=1, max_length=50)
    mandatory_output_fields: list[str] = Field(min_length=1, max_length=100)
    optional_output_fields: list[str] = Field(default_factory=list, max_length=100)
    allowed_missingness: dict[str, float] = Field(default_factory=dict)
    minimum_evidence_requirements: list[str] = Field(min_length=1, max_length=50)
    minimum_coverage_requirements: dict[str, float | int | str] = Field(default_factory=dict)
    minimum_class_size_requirements: dict[str, int] = Field(default_factory=dict)
    permitted_biological_contexts: list[str] = Field(default_factory=list, max_length=50)
    excluded_modalities: list[str] = Field(default_factory=list, max_length=50)
    intended_scope_of_claim: str = Field(min_length=10, max_length=4000)
    assumptions_requiring_human_approval: list[str] = Field(default_factory=list, max_length=50)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    requires_human_review: bool = True

    @model_validator(mode="after")
    def require_identity_and_structure(self) -> TrainingDatasetSpecification:
        identity = {item.casefold() for item in self.compound_identity_requirements}
        fields = set(self.mandatory_output_fields)
        if not identity:
            raise ValueError("at least one compound identity requirement is mandatory")
        if not ({"canonical_compound_id", "inchikey", "pubchem_cid"} & fields):
            raise ValueError("mandatory output must contain a canonical compound identifier")
        if not ({"canonical_smiles", "isomeric_smiles", "inchikey"} & fields):
            raise ValueError("mandatory output must contain a chemical structure field")
        if (
            self.prediction_unit is PredictionUnit.EXPLICIT_OTHER
            and not self.explicit_prediction_grain
        ):
            raise ValueError("explicit_prediction_grain is required for explicit_other")
        invalid = {
            key: value for key, value in self.allowed_missingness.items() if not 0 <= value <= 1
        }
        if invalid:
            raise ValueError("allowed_missingness values must be between zero and one")
        return self


class ComponentRequirement(StrictContract):
    requirement_id: str = Field(min_length=3, max_length=160)
    role: ComponentRole
    mandatory: bool
    acceptable_data_forms: list[str] = Field(min_length=1, max_length=30)
    acceptable_identifier_types: list[str] = Field(default_factory=list, max_length=30)
    minimum_metadata: list[str] = Field(default_factory=list, max_length=50)
    quality_requirements: list[str] = Field(default_factory=list, max_length=50)
    possible_substitutes: list[ComponentRole] = Field(default_factory=list, max_length=20)
    depends_on: list[str] = Field(default_factory=list, max_length=30)


class TrainingDatasetComponentRequirements(StrictContract):
    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    specification_id: str
    requirements: list[ComponentRequirement] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_requirements(self) -> TrainingDatasetComponentRequirements:
        ids = [item.requirement_id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("component requirement IDs must be unique")
        known = set(ids)
        missing = sorted({dep for item in self.requirements for dep in item.depends_on} - known)
        if missing:
            raise ValueError(f"component dependencies are missing: {', '.join(missing)}")
        return self


def derive_component_requirements(
    specification: TrainingDatasetSpecification,
) -> TrainingDatasetComponentRequirements:
    """Derive the source ontology deterministically; this is not a source strategy."""

    identity_ids = list(specification.compound_identity_requirements)
    requirements = [
        ComponentRequirement(
            requirement_id="endpoint-activity",
            role=ComponentRole.ENDPOINT_ACTIVITY,
            mandatory=True,
            acceptable_data_forms=[
                item.value for item in specification.acceptable_activity_representations
            ],
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["assay_id", "measurement_type", "assay_context"],
            quality_requirements=["primary public records", "explicit endpoint modality"],
        ),
        ComponentRequirement(
            requirement_id="assay-metadata",
            role=ComponentRole.ASSAY_METADATA,
            mandatory=True,
            acceptable_data_forms=["structured assay metadata"],
            minimum_metadata=["biological_target", "endpoint_modality", "biological_system"],
            quality_requirements=["stable source identifier", "provenance"],
            depends_on=["endpoint-activity"],
        ),
        ComponentRequirement(
            requirement_id="compound-identity",
            role=ComponentRole.COMPOUND_IDENTITY,
            mandatory=True,
            acceptable_data_forms=["identifier table", "mapping table"],
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["source_compound_id", "canonical_compound_id"],
            quality_requirements=["deterministic mapping status", "conflict flags"],
        ),
        ComponentRequirement(
            requirement_id="chemical-structure",
            role=ComponentRole.CHEMICAL_STRUCTURE,
            mandatory=True,
            acceptable_data_forms=list(specification.chemical_structure_requirements),
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["canonical_smiles", "inchikey"],
            quality_requirements=["structure provenance", "mixture and salt flags"],
            depends_on=["compound-identity"],
        ),
        ComponentRequirement(
            requirement_id="transcriptomic-matrix",
            role=ComponentRole.TRANSCRIPTOMIC_MATRIX,
            mandatory=True,
            acceptable_data_forms=list(specification.acceptable_transcriptomic_representations),
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["feature_schema", "perturbagen_identifier"],
            quality_requirements=["chemical perturbation", "reproducible feature schema"],
            depends_on=["compound-identity"],
        ),
        ComponentRequirement(
            requirement_id="transcriptomic-conditions",
            role=ComponentRole.TRANSCRIPTOMIC_CONDITIONS,
            mandatory=True,
            acceptable_data_forms=["sample metadata", "signature metadata"],
            acceptable_identifier_types=identity_ids,
            minimum_metadata=list(specification.experimental_context_requirements),
            quality_requirements=["defined control or reference", "condition provenance"],
            depends_on=["transcriptomic-matrix"],
        ),
        ComponentRequirement(
            requirement_id="provenance-license",
            role=ComponentRole.PROVENANCE_LICENSE,
            mandatory=True,
            acceptable_data_forms=["source references", "licence metadata"],
            minimum_metadata=["official source", "retrieval artifact hash"],
            quality_requirements=["immutable evidence references"],
        ),
        ComponentRequirement(
            requirement_id="source-id-mapping",
            role=ComponentRole.SOURCE_ID_MAPPING,
            mandatory=False,
            acceptable_data_forms=["cross-reference table", "deterministic identity bridge"],
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["source_identifier", "target_identifier"],
            quality_requirements=["mapping confidence", "collision flags"],
            depends_on=["compound-identity"],
        ),
        ComponentRequirement(
            requirement_id="counter-screen",
            role=ComponentRole.COUNTER_SCREEN,
            mandatory=False,
            acceptable_data_forms=["confirmatory assay", "counter-screen assay"],
            acceptable_identifier_types=identity_ids,
            minimum_metadata=["assay_id", "relationship"],
            quality_requirements=["official source relationship"],
            depends_on=["endpoint-activity"],
        ),
    ]
    return TrainingDatasetComponentRequirements(
        specification_id=specification.specification_id,
        requirements=requirements,
    )


class SourceCapability(StrictContract):
    component: ComponentRole
    status: CapabilityStatus
    fields: list[str] = Field(default_factory=list, max_length=100)
    evidence_references: list[str] = Field(default_factory=list, max_length=100)
    limitation: str | None = Field(default=None, max_length=2000)


class VerifiedSourceRecord(StrictContract):
    source_id: str = Field(min_length=3, max_length=160)
    source_roles: list[ComponentRole] = Field(min_length=1, max_length=30)
    source_system: str = Field(min_length=2, max_length=160)
    stable_accession: str = Field(min_length=1, max_length=300)
    official_source: str = Field(min_length=2, max_length=300)
    verified_public_availability: bool
    capabilities: list[SourceCapability] = Field(min_length=1, max_length=100)
    identifier_fields: list[str] = Field(default_factory=list, max_length=100)
    structure_fields: list[str] = Field(default_factory=list, max_length=100)
    measurement_fields: list[str] = Field(default_factory=list, max_length=100)
    experimental_context_fields: list[str] = Field(default_factory=list, max_length=100)
    downloadable_artifacts: list[str] = Field(default_factory=list, max_length=100)
    access_status: CapabilityStatus
    licence_status: CapabilityStatus
    source_references: list[str] = Field(min_length=1, max_length=100)
    evidence_quality: str = Field(min_length=1, max_length=1000)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_lÛÍ¶¶‰žËkºwµç}½Ù•É±…Á}½Õ¹Ðè¥¹Ðð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°”ôÀ¤(€€€Á…ÉÑ¥…±}½Ù•É±…Á}½Õ¹Ðè¥¹Ðð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°”ôÀ¤(€€€…Ñ¥Ù¥Ñå}½Ù•É…”è™±½…Ðð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°”ôÀ°±”ôÄ¤(€€€ÑÉ…¹ÍÉ¥ÁÑ½µ¥}½Ù•É…”è™±½…Ðð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°”ôÀ°±”ôÄ¤(€€€ÍÑÉÕÑÕÉ•}½Ù•É…”è™±½…Ðð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°”ôÀ°±”ôÄ¤(€€€±…ÍÍ}½Õ¹ÑÌè‘¥ÑmÍÑÈ°¥¹Ñt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ‘¥Ð¤(€€€™•…ÑÕÉ•}Í¡•µ…}½µÁ…Ñ¥‰±”è‰½½°ð9½¹”€ô9½¹”(€€€½¹‘¥Ñ¥½¹}½µÁ…Ñ¥‰¥±¥ÑäèÍÑÈð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°µ…á}±•¹Ñ ôÈÀÀÀ¤(€€€É•ÅÕ¥É•‘}‘½Ý¹±½…‘Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€É•ÅÕ¥É•‘}½µÁÕÑ…Ñ¥½¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€•Ù¥‘•¹•}É•™•É•¹•Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€±¥µ¥Ñ…Ñ¥½¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤((€€€µ½‘•±}Ù…±¥‘…Ñ½È¡µ½‘”ô‰…™Ñ•Èˆ¤(€€€‘•˜•á…Ñ}Ù…±Õ•Í}É•ÅÕ¥É•}•á…Ñ}ÍÑ…ÑÕÌ¡Í•±˜¤€´ø)½¥¹…‰¥±¥Ñå¥…¹½ÍÑ¥Œè(€€€€€€€¥˜€ (€€€€€€€€€€€Í•±˜¹ÍÑ…ÑÕÌ¥Ì¹½Ð)½¥¹…‰¥±¥ÑåMÑ…ÑÕÌ¹=5AUQ}aP(€€€€€€€€€€€…¹Í•±˜¹•á…Ñ}½Ù•É±…Á}½Õ¹Ð¥Ì¹½Ð9½¹”(€€€€€€€€¤è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰•á…Ð½Ù•É±…Àµ…ä‰”É•Á½ÉÑ•½¹±ä™½È½µÁÕÑ•‘}•á…Ð‘¥…¹½ÍÑ¥Ìˆ¤(€€€€€€€¥˜Í•±˜¹ÍÑ…ÑÕÌ¥Ì)½¥¹…‰¥±¥ÑåMÑ…ÑÕÌ¹=5AUQ}aP…¹Í•±˜¹•á…Ñ}½Ù•É±…Á}½Õ¹Ð¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰½µÁÕÑ•‘}•á…Ð‘¥…¹½ÍÑ¥ÌÉ•ÅÕ¥É”•á…Ñ}½Ù•É±…Á}½Õ¹Ðˆ¤(€€€€€€€É•ÑÕÉ¸Í•±˜(()±…ÍÌQÉ…¥¹¥¹…Ñ…Í•ÑAÉ•Á…É…Ñ¥½¹MÑ•À¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€ÍÑ•Á}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÈ°µ…á}±•¹Ñ ôÄØÀ¤(€€€½É‘•Èè¥¹Ð€ô¥•±¡”ôÄ¤(€€€…Ñ¥½¸èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÈÀÀÀ¤(€€€ÍÑ…ÑÕÌèAÉ•Á…É…Ñ¥½¹MÑ•ÁMÑ…ÑÕÌ(€€€Í½ÕÉ•}¥‘Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€Ñ…É•Ñ}™¥•±‘Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€•Ù¥‘•¹•}É•™•É•¹•Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€‰±½­•ÈèÍÑÈð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°µ…á}±•¹Ñ ôÈÀÀÀ¤(()±…ÍÌQÉ…¥¹¥¹…Ñ…Í•ÑAÉ•Á…É…Ñ¥½¹A±…¸¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€Á±…¹}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€ÍÑÉ…Ñ•å}¥èÍÑÈ(€€€ÍÑ•ÁÌè±¥ÍÑmQÉ…¥¹¥¹…Ñ…Í•ÑAÉ•Á…É…Ñ¥½¹MÑ•Át€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôÈÀÀ¤(€€€É•ÅÕ¥É•Í}¡Õµ…¹}…ÁÁÉ½Ù…±}‰•™½É•}ÑÉ…¥¹¥¹œè‰½½°€ôQÉÕ”((€€€µ½‘•±}Ù…±¥‘…Ñ½È¡µ½‘”ô‰…™Ñ•Èˆ¤(€€€‘•˜½É‘•É•‘}ÍÑ•ÁÌ¡Í•±˜¤€´øQÉ…¥¹¥¹…Ñ…Í•ÑAÉ•Á…É…Ñ¥½¹A±…¸è(€€€€€€€½É‘•ÉÌ€ôm¥Ñ•´¹½É‘•È™½È¥Ñ•´¥¸Í•±˜¹ÍÑ•ÁÍt(€€€€€€€¥˜±•¸¡½É‘•ÉÌ¤€„ô±•¸¡Í•Ð¡½É‘•ÉÌ¤¤½ÈÍ½ÉÑ•¡½É‘•ÉÌ¤€„ô±¥ÍÐ¡É…¹” Ä°±•¸¡½É‘•ÉÌ¤€¬€Ä¤¤è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰ÁÉ•Á…É…Ñ¥½¸ÍÑ•ÁÌµÕÍÐ¡…Ù”Õ¹¥ÅÕ”½¹Ñ¥Õ½ÕÌ½É‘•ÈÙ…±Õ•Ìˆ¤(€€€€€€€É•ÑÕÉ¸Í•±˜(()±…ÍÌQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åMÑÉ…Ñ•ä¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€½¹ÑÉ…Ñ}Ù•ÉÍ¥½¸è1¥Ñ•É…±lˆÄ¸À¸À‰t€ôQI%9%9}QMQ}=9QIQ}YIM%=8(€€€ÍÑÉ…Ñ•å}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€Ñ…É•Ñ}ÍÁ•¥™¥…Ñ¥½¹}¥èÍÑÈ(€€€Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}¥èÍÑÈ(€€€Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}Ù•ÉÍ¥½¸è¥¹Ð€ô¥•±¡”ôÄ¤(€€€Í½ÕÉ•}É…Á èQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åÉ…Á (€€€Í½ÕÉ•}É½±•Ìè‘¥ÑmÍÑÈ°±¥ÍÑm½µÁ½¹•¹ÑI½±•ut(€€€ÑÉ…¹Í™½Éµ…Ñ¥½¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÈÀÀ¤(€€€©½¥¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÈÀÀ¤(€€€¥‘•¹Ñ¥Ñå}Á½±¥äèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€¡•µ¥…±}ÍÑ…¹‘…É‘¥é…Ñ¥½¹}Á½±¥äèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€±…‰•±}Á½±¥äèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€ÑÉ…¹ÍÉ¥ÁÑ½µ¥}½¹‘¥Ñ¥½¹}Á½±¥äèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€É•Á•…Ñ•‘}Í¥¹…ÑÕÉ•}Á½±¥äèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€•áÁ•Ñ•‘}½ÕÑÁÕÑ}É…¥¸èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄÀÀÀ¤(€€€½Ù•É±…Á}‘¥…¹½ÍÑ¥Œè)½¥¹…‰¥±¥Ñå¥…¹½ÍÑ¥Œ(€€€•áÁ•Ñ•‘}½Ù•É…”è‘¥ÑmÍÑÈ°™±½…Ðð¥¹ÐðÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ‘¥Ð¤(€€€•áÁ•Ñ•‘}±…ÍÍ}‰…±…¹”è‘¥ÑmÍÑÈ°¥¹Ðð™±½…ÐðÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ‘¥Ð¤(€€€•Ù¥‘•¹•}ÅÕ…±¥ÑäèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€½µÁÕÑ…Ñ¥½¹…±}É•ÅÕ¥É•µ•¹ÑÌè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€ÁÉ•Á…É…Ñ¥½¹}•™™½ÉÐèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôÄÀÀÀ¤(€€€Í¥•¹Ñ¥™¥}É¥Í­Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€Ñ•¡¹¥…±}É¥Í­Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€±¥•¹Í¥¹}…•ÍÍ}É¥Í­Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€Õ¹É•Í½±Ù•‘}ÅÕ•ÍÑ¥½¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€µ¥ÍÍ¥¹}½µÁ½¹•¹ÑÌè±¥ÍÑm½µÁ½¹•¹ÑI½±•t€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÔÀ¤(€€€Ñ…É•Ñ•‘}™½±±½Ý}ÕÁ}Í•…É¡}É•ÅÕ•ÍÑÌè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÔÀ¤(€€€™…±±‰…­}ÍÑÉ…Ñ•å}¥èÍÑÈð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°µ…á}±•¹Ñ ôÄØÀ¤(€€€ÁÉ•Á…É…Ñ¥½¹}Á±…¸èQÉ…¥¹¥¹…Ñ…Í•ÑAÉ•Á…É…Ñ¥½¹A±…¸(€€€ÍÑ…ÑÕÌèMÑÉ…Ñ•åMÑ…ÑÕÌ(€€€É•ÅÕ¥É•Í}¡Õµ…¹}É•Ù¥•Üè‰½½°€ôQÉÕ”((€€€‘•˜Ù…±¥‘…Ñ•}¥¹Ù•¹Ñ½Éä¡Í•±˜°¥¹Ù•¹Ñ½ÉäèY•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½Éä¤€´ø9½¹”è(€€€€€€€¥˜€ (€€€€€€€€€€€Í•±˜¹Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}¥€„ô¥¹Ù•¹Ñ½Éä¹¥¹Ù•¹Ñ½Éå}¥(€€€€€€€€€€€½ÈÍ•±˜¹Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}Ù•ÉÍ¥½¸€„ô¥¹Ù•¹Ñ½Éä¹Ù•ÉÍ¥½¸(€€€€€€€€¤è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰…ÍÍ•µ‰±äÍÑÉ…Ñ•ä¥¹Ù•¹Ñ½Éä‰¥¹‘¥¹œ¥ÌÍÑ…±”ˆ¤(€€€€€€€Í•±˜¹Í½ÕÉ•}É…Á ¹Ù…±¥‘…Ñ•}¥¹Ù•¹Ñ½Éä¡¥¹Ù•¹Ñ½Éä¤(€€€€€€€Õ¹‘¥Í½Ù•É•€ôÍ½ÉÑ•¡Í•Ð¡Í•±˜¹Í½ÕÉ•}É½±•Ì¤€´¥¹Ù•¹Ñ½Éä¹Í½ÕÉ•}¥‘Ì¤(€€€€€€€¥˜Õ¹‘¥Í½Ù•É•è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È¡˜‰ÍÑÉ…Ñ•äÉ•™•É•¹•ÌÕ¹‘¥Í½Ù•É•Í½ÕÉ•Ìèìœ°€œ¹©½¥¸¡Õ¹‘¥Í½Ù•É•¥ôˆ¤(()±…ÍÌÍÍ•µ‰±å…À¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€…Á}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€½µÁ½¹•¹Ðè½µÁ½¹•¹ÑI½±”ð9½¹”€ô9½¹”(€€€‘•ÍÉ¥ÁÑ¥½¸èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÈÀÀÀ¤(€€€‰±½­¥¹œè‰½½°(€€€Ñ…É•Ñ•‘}Í•…É¡}É•ÅÕ•ÍÐèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÈÀÀÀ¤(€€€ÁÉ¥½É}ÅÕ•Éå}™¥¹•ÉÁÉ¥¹ÑÌè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(()±…ÍÌÍÍ•µ‰±å…ÁI•Á½ÉÐ¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€É•Á½ÉÑ}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€¥¹Ù•¹Ñ½Éå}¥èÍÑÈ(€€€¥¹Ù•¹Ñ½Éå}Ù•ÉÍ¥½¸è¥¹Ð€ô¥•±¡”ôÄ¤(€€€…ÁÌè±¥ÍÑmÍÍ•µ‰±å…Át€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€‘¥Í½Ù•Éå}É½Õ¹è¥¹Ð€ô¥•±¡”ôÀ¤(€€€µ…á¥µÕµ}‘¥Í½Ù•Éå}É½Õ¹‘Ìè¥¹Ð€ô¥•±¡”ôÀ°±”ôÄÀ¤(€€€É•ÅÕ¥É•Í}¡Õµ…¹}Í½Á•}É•Ù¥•Üè‰½½°€ô…±Í”((€€€‘•˜¹•áÑ}ÅÕ•É¥•Ì¡Í•±˜°…±É•…‘å}•á•ÕÑ•èÍ•ÑmÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè(€€€€€€€¥˜Í•±˜¹‘¥Í½Ù•Éå}É½Õ¹€øôÍ•±˜¹µ…á¥µÕµ}‘¥Í½Ù•Éå}É½Õ¹‘Ìè(€€€€€€€€€€€É•ÑÕÉ¸mt(€€€€€€€É•ÑÕÉ¸l(€€€€€€€€€€€¥Ñ•´¹Ñ…É•Ñ•‘}Í•…É¡}É•ÅÕ•ÍÐ(€€€€€€€€€€€™½È¥Ñ•´¥¸Í•±˜¹…ÁÌ(€€€€€€€€€€€¥˜¥Ñ•´¹Ñ…É•Ñ•‘}Í•…É¡}É•ÅÕ•ÍÐ¹½Ð¥¸…±É•…‘å}•á•ÕÑ•(€€€€€€€t(()±…ÍÌQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åI•Ù¥•Ü¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€É•Ù¥•Ý}¥èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€ÍÁ•¥™¥…Ñ¥½¹}¥èÍÑÈ(€€€¥¹Ù•¹Ñ½Éå}¥èÍÑÈ(€€€¥¹Ù•¹Ñ½Éå}Ù•ÉÍ¥½¸è¥¹Ð€ô¥•±¡”ôÄ¤(€€€ÍÑÉ…Ñ•¥•Ìè±¥ÍÑmQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åMÑÉ…Ñ•åt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÔÀ¤(€€€É•½µµ•¹‘•‘}ÍÑÉ…Ñ•å}¥èÍÑÈð9½¹”€ô¥•±¡‘•™…Õ±Ðõ9½¹”°µ…á}±•¹Ñ ôÄØÀ¤(€€€‘•¥Í¥½¹}ÍÕµµ…ÉäèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôàÀÀÀ¤(€€€•Ù¥‘•¹•}ÕÍ•è±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÈÀÀ¤(€€€Õ¹É•Í½±Ù•‘}ÅÕ•ÍÑ¥½¹Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€¹½}™•…Í¥‰±•}ÍÑÉ…Ñ•äè‰½½°€ô…±Í”(€€€É•ÅÕ¥É•Í}¡Õµ…¹}É•Ù¥•Üè‰½½°€ôQÉÕ”((€€€µ½‘•±}Ù…±¥‘…Ñ½È¡µ½‘”ô‰…™Ñ•Èˆ¤(€€€‘•˜Ù…±¥‘…Ñ•}É•½µµ•¹‘…Ñ¥½¸¡Í•±˜¤€´øQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åI•Ù¥•Üè(€€€€€€€¥‘Ì€ôí¥Ñ•´¹ÍÑÉ…Ñ•å}¥™½È¥Ñ•´¥¸Í•±˜¹ÍÑÉ…Ñ•¥•Íô(€€€€€€€¥˜Í•±˜¹É•½µµ•¹‘•‘}ÍÑÉ…Ñ•å}¥…¹Í•±˜¹É•½µµ•¹‘•‘}ÍÑÉ…Ñ•å}¥¹½Ð¥¸¥‘Ìè(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰É•½µµ•¹‘•ÍÑÉ…Ñ•äµÕÍÐ‰”¥¹±Õ‘•¥¸ÍÑÉ…Ñ•¥•Ìˆ¤(€€€€€€€¥˜Í•±˜¹¹½}™•…Í¥‰±•}ÍÑÉ…Ñ•ä…¹Í•±˜¹É•½µµ•¹‘•‘}ÍÑÉ…Ñ•å}¥è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰¹¼µ™•…Í¥‰±”É•Ù¥•Ü…¹¹½ÐÉ•½µµ•¹„ÍÑÉ…Ñ•äˆ¤(€€€€€€€É•ÑÕÉ¸Í•±˜(()±…ÍÌ	±¥¹‘	•¹¡µ…É­%¹¥Ñ¥…±½¹Ñ•áÐ¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€‰•¹¡µ…É­}µ½‘”èÍÑÈ€ô¥•± (€€€€€€€‘•™…Õ±Ðõ	1%9}QI%9%9}QMQ}%M=YId°µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôÄÈÀ(€€€€¤(€€€•¹‘Á½¥¹Ñ}¹…µ”èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÌ°µ…á}±•¹Ñ ôÄØÀ¤(€€€‰¥½±½¥…±}½…°èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄÀ°µ…á}±•¹Ñ ôÐÀÀÀ¤(€€€Ñ…É•Ñ}ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}½¹ÑÉ…Ðè‘¥ÑmÍÑÈ°¹åt(€€€Í½ÕÉ•}…‘…ÁÑ•É}…Á…‰¥±¥Ñ¥•Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€…ÁÁÉ½Ù•‘}Í¥•¹Ñ¥™¥}Á½±¥¥•Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€…±±½Ý•‘}Ñ½½±Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€Á±…¹¹•É}ÁÉ½Ù¥‘•ÈèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôàÀ¤(€€€Á±…¹¹•É}µ½‘•°èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôÄØÀ¤(€€€Ý½É­•É}ÁÉ½Ù¥‘•ÈèÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôàÀ¤(€€€Ý½É­•É}µ½‘•°èÍÑÈ€ô¥•±¡µ¥¹}±•¹Ñ ôÄ°µ…á}±•¹Ñ ôÄØÀ¤(€€€‰Õ‘•ÑÌè‘¥ÑmÍÑÈ°¹åt(€€€Í½ÕÉ•}¡¥¹ÑÌè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÀ¤(€€€…ÉÑ¥±•}¡¥¹Ðè9½¹”€ô9½¹”(€€€‘½¥}¡¥¹Ðè9½¹”€ô9½¹”(€€€…ÍÍ…å}¥‘}¡¥¹Ðè9½¹”€ô9½¹”(€€€…Ñ¥Ù¥Ñå}Í½ÕÉ•}¡¥¹Ðè9½¹”€ô9½¹”(€€€ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•}¡¥¹Ðè9½¹”€ô9½¹”(€€€•áÁ•Ñ•‘}½Õ¹Ñ}¡¥¹Ðè9½¹”€ô9½¹”(€€€•áÁ•Ñ•‘}½Ù•É±…Á}¡¥¹Ðè9½¹”€ô9½¹”(€€€•¹‘Á½¥¹Ñ}ÍÁ•¥™¥}µ…ÁÁ¥¹}¡¥¹Ðè9½¹”€ô9½¹”((€€€µ½‘•±}Ù…±¥‘…Ñ½È¡µ½‘”ô‰…™Ñ•Èˆ¤(€€€‘•˜™½É‰¥‘}Í½ÕÉ•}¡¥¹ÑÌ¡Í•±˜¤€´ø	±¥¹‘	•¹¡µ…É­%¹¥Ñ¥…±½¹Ñ•áÐè(€€€€€€€¥˜Í•±˜¹‰•¹¡µ…É­}µ½‘”€„ô	1%9}QI%9%9}QMQ}%M=YIdè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜(€€€€€€€¥˜Í•±˜¹Í½ÕÉ•}¡¥¹ÑÌè(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰±¥¹‰•¹¡µ…É¬½¹Ñ•áÐµÕÍÐ¹½Ð½¹Ñ…¥¸Í½ÕÉ”¡¥¹ÑÌˆ¤(€€€€€€€Í•É¥…±¥é•€ôÍ•±˜¹µ½‘•±}‘ÕµÁ}©Í½¸ ¤¹…Í•™½± ¤(€€€€€€€™½É‰¥‘‘•¸€ô€ ‰‘½¤¹½Éœ¼ˆ°€‰ÁÕ‰µ•ˆ°€‰‰¥½…ÍÍ…ä¥ˆ°€‰­¹½Ý¸…•ÍÍ¥½¸ˆ¤(€€€€€€€¥˜…¹ä¡Ñ½­•¸¥¸Í•É¥…±¥é•™½ÈÑ½­•¸¥¸™½É‰¥‘‘•¸¤è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰±¥¹‰•¹¡µ…É¬½¹Ñ•áÐ½¹Ñ…¥¹Ì„ÁÉ½¡¥‰¥Ñ•Í½ÕÉ”¡¥¹Ðˆ¤(€€€€€€€É•ÑÕÉ¸Í•±˜(()±…ÍÌMÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸¡MÑÉ¥Ñ½¹ÑÉ…Ð¤è(€€€…•¹Ñ}¹…µ”èÍÑÈ(€€€É½±”è1¥Ñ•É…±l‰Á±…¹¹•Èˆ°€‰Ý½É­•È‰t(€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”èÍÑÈ(€€€…±±½Ý•‘}Ñ½½±Ìè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌè±¥ÍÑmÍÑÉt€ô¥•±¡‘•™…Õ±Ñ}™…Ñ½Éäõ±¥ÍÐ°µ…á}±•¹Ñ ôÄÀÀ¤(€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…ÐèÍÑÈ(()MA%1%i}9Q}MEU9€ôl(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰…Ñ…Í•ÐMÁ•¥™¥…Ñ¥½¸•¹Ðˆ°(€€€€€€€É½±”ô‰Á±…¹¹•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰QÉ…¥¹¥¹…Ñ…Í•ÑMÁ•¥™¥…Ñ¥½¸ˆ°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl‰•¹‘Á½¥¹Ñ}‘•™¥¹¥Ñ¥½¸‰t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}ÍÁ•¥™¥…Ñ¥½¸ˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰Ñ¥Ù¥ÑäÙ¥‘•¹”¥Í½Ù•Éä•¹Ðˆ°(€€€€€€€É½±”ô‰Ý½É­•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰Y•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½ÉåÉ…µ•¹Ðˆ°(€€€€€€€…±±½Ý•‘}Ñ½½±Ìõl(€€€€€€€€€€€€‰Í•…É¡}…Ñ¥Ù¥Ñå}Í½ÕÉ•Ìˆ°(€€€€€€€€€€€€‰Ù…±¥‘…Ñ•}…Ñ¥Ù¥Ñå}Í½ÕÉ”ˆ°(€€€€€€€€€€€€‰™•Ñ¡}…Ñ¥Ù¥Ñå}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}…Ñ¥Ù¥Ñå}É•ÍÕ±Ñ}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}…Ñ¥Ù¥Ñå}¥‘•¹Ñ¥™¥•É}™¥•±‘Ìˆ°(€€€€€€€€€€€€‰ÍÕµµ…É¥é•}…Ñ¥Ù¥Ñå}½ÕÑ½µ•Ìˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}½Õ¹Ñ•É}ÍÉ••¹}É•±…Ñ¥½¹Í¡¥ÁÌˆ°(€€€€€€€t°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl‰ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}ÍÁ•¥™¥…Ñ¥½¸ˆ°€‰½µÁ½¹•¹Ñ}É•ÅÕ¥É•µ•¹ÑÌ‰t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰…Ñ¥Ù¥Ñå}Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}™É…µ•¹Ðˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰QÉ…¹ÍÉ¥ÁÑ½µ¥ŒÙ¥‘•¹”¥Í½Ù•Éä•¹Ðˆ°(€€€€€€€É½±”ô‰Ý½É­•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰Y•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½ÉåÉ…µ•¹Ðˆ°(€€€€€€€…±±½Ý•‘}Ñ½½±Ìõl(€€€€€€€€€€€€‰Í•…É¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•Ìˆ°(€€€€€€€€€€€€‰Ù…±¥‘…Ñ•}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ”ˆ°(€€€€€€€€€€€€‰™•Ñ¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Á•ÉÑÕÉ‰…Ñ¥½¹}‘•Í¥¸ˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}¥‘•¹Ñ¥Ñå}™¥•±‘Ìˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Í¥¹…ÑÕÉ•}½¹‘¥Ñ¥½¹Ìˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}ÁÉ½•ÍÍ•‘}µ…ÑÉ¥á}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}É…Ý}µ…ÑÉ¥á}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}™•…ÑÕÉ•}Í¡•µ„ˆ°(€€€€€€€t°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl‰ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}ÍÁ•¥™¥…Ñ¥½¸ˆ°€‰½µÁ½¹•¹Ñ}É•ÅÕ¥É•µ•¹ÑÌ‰t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}™É…µ•¹Ðˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰¡•µ¥…°%‘•¹Ñ¥Ñä…¹MÑÉÕÑÕÉ”M½ÕÉ”¥Í½Ù•Éä•¹Ðˆ°(€€€€€€€É½±”ô‰Ý½É­•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰Y•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½ÉåÉ…µ•¹Ðˆ°(€€€€€€€…±±½Ý•‘}Ñ½½±Ìõl(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Í½ÕÉ•}¥‘•¹Ñ¥Ñå}™¥•±‘Ìˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Í½ÕÉ•}É•½É‘}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€€€€€‰‰Õ¥±‘}½µÁ½Õ¹‘}µ…ÁÁ¥¹}µ…¹¥™•ÍÐˆ°(€€€€€€€€€€€€‰É•Í½±Ù•}½µÁ½Õ¹‘}¥‘•¹Ñ¥Ñå}Í…µÁ±”ˆ°(€€€€€€€t°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl‰½µÁ½¹•¹Ñ}É•ÅÕ¥É•µ•¹ÑÌˆ°€‰‘¥Í½Ù•É•‘}Í½ÕÉ•}™É…µ•¹ÑÌ‰t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰¥‘•¹Ñ¥Ñå}Í½ÕÉ•}¥¹Ù•¹Ñ½Éå}™É…µ•¹Ðˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰MÕÁÁ½ÉÑ¥¹œ5•Ñ…‘…Ñ„¥Í½Ù•Éä•¹Ðˆ°(€€€€€€€É½±”ô‰Ý½É­•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰Y•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½ÉåÉ…µ•¹Ðˆ°(€€€€€€€…±±½Ý•‘}Ñ½½±Ìõl(€€€€€€€€€€€€‰™•Ñ¡}…Ñ¥Ù¥Ñå}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰™•Ñ¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Í¥¹…ÑÕÉ•}½¹‘¥Ñ¥½¹Ìˆ°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}Í½ÕÉ•}É•½É‘}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€t°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl‰½µÁ½¹•¹Ñ}É•ÅÕ¥É•µ•¹ÑÌˆ°€‰‘¥Í½Ù•É•‘}Í½ÕÉ•}™É…µ•¹ÑÌ‰t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰ÍÕÁÁ½ÉÑ¥¹}µ•Ñ…‘…Ñ…}¥¹Ù•¹Ñ½Éå}™É…µ•¹Ðˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰QÉ…¥¹¥¹œ…Ñ…Í•ÐÍÍ•µ‰±äMÑÉ…Ñ•äA±…¹¹•Èˆ°(€€€€€€€É½±”ô‰Á±…¹¹•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰QÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åI•Ù¥•Üˆ°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl(€€€€€€€€€€€€‰ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}ÍÁ•¥™¥…Ñ¥½¸ˆ°(€€€€€€€€€€€€‰½µÁ½¹•¹Ñ}É•ÅÕ¥É•µ•¹ÑÌˆ°(€€€€€€€€€€€€‰Ù•É¥™¥•‘}Í½ÕÉ•}¥¹Ù•¹Ñ½Éäˆ°(€€€€€€€€€€€€‰Í½ÕÉ•}…Á…‰¥±¥Ñå}µ…ÑÉ¥àˆ°(€€€€€€€€€€€€‰©½¥¹…‰¥±¥Ñå}‘¥…¹½ÍÑ¥Ìˆ°(€€€€€€€t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰…ÍÍ•µ‰±å}ÍÑÉ…Ñ•å}É•Ù¥•Üˆ°(€€€€¤°(€€€MÁ•¥…±¥é•‘•¹Ñ•™¥¹¥Ñ¥½¸ (€€€€€€€…•¹Ñ}¹…µ”ô‰ÍÍ•µ‰±äMÑÉ…Ñ•äÙ…±Õ…Ñ¥½¸•¹Ðˆ°(€€€€€€€É½±”ô‰Á±…¹¹•Èˆ°(€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”ô‰QÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åI•Ù¥•Üˆ°(€€€€€€€É••¥Ù•Í}…ÉÑ¥™…ÑÌõl(€€€€€€€€€€€€‰ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}ÍÁ•¥™¥…Ñ¥½¸ˆ°(€€€€€€€€€€€€‰Ù•É¥™¥•‘}Í½ÕÉ•}¥¹Ù•¹Ñ½Éäˆ°(€€€€€€€€€€€€‰Í½ÕÉ•}…Á…‰¥±¥Ñå}µ…ÑÉ¥àˆ°(€€€€€€€€€€€€‰…ÍÍ•µ‰±å}ÍÑÉ…Ñ•¥•Ìˆ°(€€€€€€€€€€€€‰©½¥¹…‰¥±¥Ñå}‘¥…¹½ÍÑ¥Ìˆ°(€€€€€€€t°(€€€€€€€ÁÉ½‘Õ•Í}…ÉÑ¥™…Ðô‰…ÍÍ•µ‰±å}ÍÑÉ…Ñ•å}½µÁ…É¥Í½¸ˆ°(€€€€¤°)t(()±…ÍÌ¥Í½Ù•Éå	•™½É•MÑÉ…Ñ•åÕ…Éè(€€€€ˆˆ‰ÕÑ¡½É¥Ñ…Ñ¥Ù”‘•Ñ•Éµ¥¹¥ÍÑ¥ŒÕ…ÉÕÍ•‰•™½É”…¹ä½¹É•Ñ”ÍÑÉ…Ñ•äÉÕ¸¸ˆˆˆ((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜Ù…±¥‘…Ñ” (€€€€€€€ÍÁ•¥™¥…Ñ¥½¸èQÉ…¥¹¥¹…Ñ…Í•ÑMÁ•¥™¥…Ñ¥½¸ð9½¹”°(€€€€€€€É•ÅÕ¥É•µ•¹ÑÌèQÉ…¥¹¥¹…Ñ…Í•Ñ½µÁ½¹•¹ÑI•ÅÕ¥É•µ•¹ÑÌð9½¹”°(€€€€€€€¥¹Ù•¹Ñ½ÉäèY•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½Éäð9½¹”°(€€€€€€€…Á…‰¥±¥Ñå}µ…ÑÉ¥àèM½ÕÉ•…Á…‰¥±¥Ñå5…ÑÉ¥àð9½¹”°(€€€€¤€´ø9½¹”è(€€€€€€€¥˜ÍÁ•¥™¥…Ñ¥½¸¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰Ñ…É•ÐÑÉ…¥¹¥¹œµ‘…Ñ…Í•ÐÍÁ•¥™¥…Ñ¥½¸¥ÌÉ•ÅÕ¥É•‰•™½É”Á±…¹¹¥¹œˆ¤(€€€€€€€¥˜É•ÅÕ¥É•µ•¹ÑÌ¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰½µÁ½¹•¹ÐÉ•ÅÕ¥É•µ•¹ÑÌ…É”É•ÅÕ¥É•‰•™½É”Á±…¹¹¥¹œˆ¤(€€€€€€€¥˜¥¹Ù•¹Ñ½Éä¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰Ù•É¥™¥•Í½ÕÉ”¥¹Ù•¹Ñ½Éä¥ÌÉ•ÅÕ¥É•‰•™½É”Á±…¹¹¥¹œˆ¤(€€€€€€€¥˜…Á…‰¥±¥Ñå}µ…ÑÉ¥à¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰Í½ÕÉ”…Á…‰¥±¥Ñäµ…ÑÉ¥à¥ÌÉ•ÅÕ¥É•‰•™½É”Á±…¹¹¥¹œˆ¤(€€€€€€€¥˜…Á…‰¥±¥Ñå}µ…ÑÉ¥à¹¥¹Ù•¹Ñ½Éå}¥€„ô¥¹Ù•¹Ñ½Éä¹¥¹Ù•¹Ñ½Éå}¥è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰…Á…‰¥±¥Ñäµ…ÑÉ¥à¥Ì¹½Ð‰½Õ¹Ñ¼Ñ¡”Ù•É¥™¥•¥¹Ù•¹Ñ½Éäˆ¤(€€€€€€€¥˜…Á…‰¥±¥Ñå}µ…ÑÉ¥à¹¥¹Ù•¹Ñ½Éå}Ù•ÉÍ¥½¸€„ô¥¹Ù•¹Ñ½Éä¹Ù•ÉÍ¥½¸è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰…Á…‰¥±¥Ñäµ…ÑÉ¥à¥¹Ù•¹Ñ½Éä‰¥¹‘¥¹œ¥ÌÍÑ…±”ˆ¤(()‘•˜Ù…±¥‘…Ñ•}ÍÑÉ…Ñ•å}Í½ÕÉ•Ì (€€€ÍÑÉ…Ñ•¥•Ìè±¥ÍÑmQÉ…¥¹¥¹…Ñ…Í•ÑÍÍ•µ‰±åMÑÉ…Ñ•åt°¥¹Ù•¹Ñ½ÉäèY•É¥™¥•‘M½ÕÉ•%¹Ù•¹Ñ½Éä(¤€´ø9½¹”è(€€€™½ÈÍÑÉ…Ñ•ä¥¸ÍÑÉ…Ñ•¥•Ìè(€€€€€€€ÍÑÉ…Ñ•ä¹Ù…±¥‘…Ñ•}¥¹Ù•¹Ñ½Éä¡¥¹Ù•¹Ñ½Éä¤(