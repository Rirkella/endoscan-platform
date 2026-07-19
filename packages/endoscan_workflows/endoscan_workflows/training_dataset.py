"""Source-neutral contracts for public training-dataset discovery and assembly.

The module deliberately contains no endpoint-specific source hints and performs no
network access.  It is the validated artifact boundary between specialized agents
and the deterministic workflow orchestrator.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import AgentConfiguration
from .contracts import (
    AgentBudget,
    AgentRunRequest,
    ModelConfiguration,
    StrictContract,
    WorkflowState,
)

TRAINING_DATASET_CONTRACT_VERSION = "1.0.0"
BLIND_TRAINING_DATASET_DISCOVERY = "blind_training_dataset_discovery"
DATASET_SPECIFICATION_COMPILER_VERSION = "1.0.0"
PLATFORM_SPECIFICATION_POLICY_VERSION = "1.0.0"


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


class EndpointSemanticModality(StrEnum):
    ANTAGONISM = "antagonism"
    AGONISM = "agonism"
    BINDING = "binding"
    INHIBITION = "inhibition"
    ACTIVATION = "activation"
    SENSITIZATION = "sensitization"
    CYTOTOXICITY = "cytotoxicity"
    PATHWAY_ACTIVATION = "pathway_activation"


class CoreEndpointDefinitionStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    CONTRADICTORY = "contradictory"


class MissingCoreEndpointElement(StrEnum):
    BIOLOGICAL_TARGET_OR_PROCESS = "biological_target_or_process"
    REQUESTED_MODALITY_OR_PREDICTION_CLAIM = "requested_modality_or_prediction_claim"
    COMPOUND_LEVEL_PREDICTION_OBJECTIVE = "compound_level_prediction_objective"
    CONTRADICTORY_CORE_DEFINITION = "contradictory_core_definition"


class SpecificationFieldOrigin(StrEnum):
    USER_REQUEST = "user_request"
    SEMANTIC_HINT = "semantic_hint"
    PLATFORM_CONTRACT = "platform_contract"
    APPROVED_POLICY = "approved_policy"
    HUMAN_DECISION_REQUIRED = "human_decision_required"


class SpecificationFieldProvenance(StrictContract):
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    origins: list[SpecificationFieldOrigin] = Field(min_length=1, max_length=5)
    explanation: str = Field(min_length=3, max_length=500)


class EndpointRequestSemanticHints(StrictContract):
    """Deterministic parsing of user-authored endpoint text, never source evidence."""

    requested_endpoint_name: str = Field(min_length=3, max_length=160)
    explicit_target_terms: list[str] = Field(max_length=10)
    explicit_modality_terms: list[EndpointSemanticModality] = Field(max_length=8)
    explicit_prediction_scope_terms: list[
        Literal[
            "compound_level",
            "prediction_or_training_objective",
            "endpoint_relative_activity",
            "transcriptomic_response",
        ]
    ] = Field(max_length=4)
    target_present: bool
    modality_present: bool
    compound_level_goal_present: bool
    core_definition_status: CoreEndpointDefinitionStatus
    missing_core_elements: list[MissingCoreEndpointElement] = Field(max_length=4)


_MODALITY_PATTERNS: tuple[tuple[EndpointSemanticModality, re.Pattern[str]], ...] = (
    (EndpointSemanticModality.ANTAGONISM, re.compile(r"\bantagon(?:ist|ists|ism|istic)\b", re.I)),
    (EndpointSemanticModality.AGONISM, re.compile(r"\bagon(?:ist|ists|ism|istic)\b", re.I)),
    (EndpointSemanticModality.BINDING, re.compile(r"\bbind(?:ing|er|ers|s)?\b", re.I)),
    (EndpointSemanticModality.INHIBITION, re.compile(r"\binhibit(?:or|ors|ion|ing|s)?\b", re.I)),
    (EndpointSemanticModality.ACTIVATION, re.compile(r"\bactivat(?:or|ors|ion|ing|es?)\b", re.I)),
    (
        EndpointSemanticModality.SENSITIZATION,
        re.compile(r"\bsensiti[sz](?:er|ers|ation|ing)\b", re.I),
    ),
    (EndpointSemanticModality.CYTOTOXICITY, re.compile(r"\bcytotoxic(?:ity)?\b", re.I)),
    (
        EndpointSemanticModality.PATHWAY_ACTIVATION,
        re.compile(r"\bpathway\s+activat(?:ion|or|ing|es?)\b", re.I),
    ),
)

_GENERIC_TARGET_WORDS = {
    "activity",
    "compound",
    "compounds",
    "effect",
    "effects",
    "enzyme",
    "hormonal",
    "modality",
    "pathway",
    "receptor",
    "response",
    "toxicity",
}


def derive_endpoint_request_semantic_hints(
    endpoint_name: str,
    biological_goal: str,
) -> EndpointRequestSemanticHints:
    """Extract bounded core semantics from only the two user-authored request fields."""

    endpoint = " ".join(endpoint_name.strip().split())
    combined = f"{endpoint} {biological_goal}".casefold()
    matches: list[tuple[int, EndpointSemanticModality]] = []
    for modality, pattern in _MODALITY_PATTERNS:
        match = pattern.search(endpoint)
        if match:
            matches.append((match.start(), modality))
    matches.sort(key=lambda item: item[0])
    modalities = list(dict.fromkeys(modality for _position, modality in matches))
    if EndpointSemanticModality.PATHWAY_ACTIVATION in modalities:
        modalities = [
            modality
            for modality in modalities
            if modality is not EndpointSemanticModality.ACTIVATION
        ]

    target_candidate = endpoint[: matches[0][0]] if matches else endpoint
    target_candidate = re.sub(
        r"\b(?:predict|prediction|compound-level|compound|chemical)\b",
        " ",
        target_candidate,
        flags=re.I,
    )
    target_candidate = " ".join(re.findall(r"[A-Za-z0-9-]+", target_candidate)).strip()
    target_tokens = {token.casefold() for token in target_candidate.split()}
    target_present = bool(target_candidate) and not target_tokens.issubset(_GENERIC_TARGET_WORDS)
    target_terms = [target_candidate] if target_present else []

    scope_terms: list[str] = []
    compound_goal = bool(re.search(r"\b(?:compound|compounds|chemical|chemicals)\b", combined))
    if compound_goal:
        scope_terms.append("compound_level")
    if re.search(r"\b(?:predict|prediction|classifier|training\s+dataset|model)\b", combined):
        scope_terms.append("prediction_or_training_objective")
    if re.search(r"\b(?:endpoint|activity|active|inactive|potency|efficacy)\b", combined):
        scope_terms.append("endpoint_relative_activity")
    if re.search(r"\b(?:transcriptomic|gene[- ]expression|expression\s+response)\b", combined):
        scope_terms.append("transcriptomic_response")

    contradictory = (
        {EndpointSemanticModality.ANTAGONISM, EndpointSemanticModality.AGONISM} <= set(modalities)
        or {EndpointSemanticModality.INHIBITION, EndpointSemanticModality.ACTIVATION}
        <= set(modalities)
        or ("all modalities" in combined and bool(modalities))
    )
    missing: list[MissingCoreEndpointElement] = []
    if not target_present:
        missing.append(MissingCoreEndpointElement.BIOLOGICAL_TARGET_OR_PROCESS)
    if not modalities:
        missing.append(MissingCoreEndpointElement.REQUESTED_MODALITY_OR_PREDICTION_CLAIM)
    if not compound_goal:
        missing.append(MissingCoreEndpointElement.COMPOUND_LEVEL_PREDICTION_OBJECTIVE)
    if contradictory:
        missing.append(MissingCoreEndpointElement.CONTRADICTORY_CORE_DEFINITION)
    status = (
        CoreEndpointDefinitionStatus.CONTRADICTORY
        if contradictory
        else CoreEndpointDefinitionStatus.INSUFFICIENT
        if missing
        else CoreEndpointDefinitionStatus.SUFFICIENT
    )
    return EndpointRequestSemanticHints(
        requested_endpoint_name=endpoint,
        explicit_target_terms=target_terms,
        explicit_modality_terms=modalities,
        explicit_prediction_scope_terms=scope_terms,
        target_present=target_present,
        modality_present=bool(modalities),
        compound_level_goal_present=compound_goal,
        core_definition_status=status,
        missing_core_elements=missing,
    )


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


class TrainingDatasetSpecificationDraft(BaseModel):
    """Strict planner proposal containing only choices available before review.

    Deliberately excludes dynamic maps and numeric coverage/class thresholds. Those
    remain part of the approved contract and are populated deterministically from
    explicit policy or left empty when no policy has supplied them.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0.0"]
    endpoint_name: str = Field(min_length=3, max_length=160)
    biological_target: str = Field(min_length=1, max_length=500)
    endpoint_modality: str = Field(min_length=1, max×Î»æÚ$z{-®éÜj×ÖVæFVE÷7G&FVw•ö–BæB6VÆbç&V6öÖÖVæFVE÷7G&FVw•ö–Bæ÷B–â–G3 ¢&—6RfÇVTW'&÷"‚'&V6öÖÖVæFVB7G&FVw’×W7B&R–æ6ÇVFVB–â7G&FVv–W2"¢–b6VÆbææõöfV6–&ÆU÷7G&FVw’æB6VÆbç&V6öÖÖVæFVE÷7G&FVw•ö–C ¢&—6RfÇVTW'&÷"‚&æòÖfV6–&ÆR&Wf–Wr6ææ÷B&V6öÖÖVæB7G&FVw’"¢&WGW&â6VÆ`  ¦6Æ72&Æ–æD&Væ6†Ö&´–æ—F–Ä6öçFW‡B…7G&–7D6öçG&7B“ ¢&Væ6†Ö&µöÖöFS¢7G"Òf–VÆB€¢FVfVÇCÔ$Ä”äEõE$”ä”äuôDD4UEôD•44õdU%’ÂÖ–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓ# ¢¢VæGö–çEöæÖS¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓ2ÂÖ…öÆVæwFƒÓc¢&–öÆöv–6ÅövöÃ¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓC¢F&vWE÷G&–æ–æuöFF6WEö6öçG&7C¢F–7E·7G"Âç•Ð¢6÷W&6UöFFW%ö6&–Æ—F–W3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢&÷fVE÷66–VçF–f–5÷öÆ–6–W3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢ÆÆ÷vVE÷FööÇ3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢ÆææW%÷&÷f–FW#¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓƒ¢ÆææW%öÖöFVÃ¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓc¢v÷&¶W%÷&÷f–FW#¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓƒ¢v÷&¶W%öÖöFVÃ¢7G"Òf–VÆB†Ö–åöÆVæwFƒÓÂÖ…öÆVæwFƒÓc¢'VFvWG3¢F–7E·7G"Âç•Ð¢VæGö–çE÷&WVW7E÷6VÖçF–5ö†–çG3¢VæGö–çE&WVW7E6VÖçF–4†–çG2ÂæöæRÒæöæP¢6÷W&6Uö†–çG3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢'F–6ÆUö†–çC¢æöæRÒæöæP¢Fö•ö†–çC¢æöæRÒæöæP¢76•ö–Eö†–çC¢æöæRÒæöæP¢7F—f—G•÷6÷W&6Uö†–çC¢æöæRÒæöæP¢G&ç67&—FöÖ–5÷6÷W&6Uö†–çC¢æöæRÒæöæP¢W‡V7FVEö6÷VçEö†–çC¢æöæRÒæöæP¢W‡V7FVEö÷fW&Æö†–çC¢æöæRÒæöæP¢VæGö–çE÷7V6–f–5öÖ–æuö†–çC¢æöæRÒæöæP ¢ÖöFVÅ÷fÆ–FF÷"†ÖöFSÒ&gFW""¢FVbf÷&&–E÷6÷W&6Uö†–çG2‡6VÆb’Óâ&Æ–æD&Væ6†Ö&´–æ—F–Ä6öçFW‡C ¢–b6VÆbæ&Væ6†Ö&µöÖöFRÒ$Ä”äEõE$”ä”äuôDD4UEôD•44õdU%“ ¢&WGW&â6VÆ`¢–b6VÆbç6÷W&6Uö†–çG3 ¢&—6RfÇVTW'&÷"‚&&Æ–æB&Væ6†Ö&²6öçFW‡B×W7Bæ÷B6öçF–â6÷W&6R†–çG2"¢6W&–Æ—¦VBÒ6VÆbæÖöFVÅöGV×ö§6öâ‚’æ66VföÆB‚¢f÷&&–FFVâÒ‚&Fö’æ÷&rò"Â'V&ÖVB"Â&&–ö76’–B"Â&¶æ÷vâ66W76–öâ"¢–bç’‡Fö¶Vâ–â6W&–Æ—¦VBf÷"Fö¶Vâ–âf÷&&–FFVâ“ ¢&—6RfÇVTW'&÷"‚&&Æ–æB&Væ6†Ö&²6öçFW‡B6öçF–ç2&ö†–&—FVB6÷W&6R†–çB"¢&WGW&â6VÆ`  ¦6Æ727V6–Æ—¦VDvVçDFVf–æ—F–öâ…7G&–7D6öçG&7B“ ¢vVçEöæÖS¢7G ¢&öÆS¢Æ—FW&Å²'ÆææW""Â'v÷&¶W"%Ð¢÷WGWE÷66†VÖöæÖS¢7G ¢ÆÆ÷vVE÷FööÇ3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢&V6V—fW5ö'F–f7G3¢Æ—7E·7G%ÒÒf–VÆB†FVfVÇEöf7F÷'“ÖÆ—7BÂÖ…öÆVæwFƒÓ¢&öGV6W5ö'F–f7C¢7G   ¥5T4”Ä•¤TEôtTåEõ4UTTä4RÒ°¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ$FF6WB7V6–f–6F–öâ&Wf–WrvVçB"À¢&öÆSÒ'ÆææW""À¢÷WGWE÷66†VÖöæÖSÒ$FF6WE7V6–f–6F–öå&Wf–Wt÷WF6öÖR"À¢&V6V—fW5ö'F–f7G3Õ°¢&VæGö–çE÷&WVW7E÷6VÖçF–5ö†–çG2"À¢'G&–æ–æuöFF6WE÷7V6–f–6F–öåöG&gB"À¢ÒÀ¢&öGV6W5ö'F–f7CÒ&FF6WE÷7V6–f–6F–öå÷&Wf–Wuö÷WF6öÖR"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ$7F—f—G’Wf–FVæ6RF—66÷fW'’vVçB"À¢&öÆSÒ'v÷&¶W""À¢÷WGWE÷66†VÖöæÖSÒ%fW&–f–VE6÷W&6T–çfVçF÷'”g&vÖVçB"À¢ÆÆ÷vVE÷FööÇ3Õ°¢'6V&6…ö7F—f—G•÷6÷W&6W2"À¢'fÆ–FFUö7F—f—G•÷6÷W&6R"À¢&fWF6…ö7F—f—G•÷6÷W&6UöÖWFFF"À¢&–ç7V7Eö7F—f—G•÷&W7VÇEöf–Æ&–Æ—G’"À¢&–ç7V7Eö7F—f—G•ö–FVçF–f–W%öf–VÆG2"À¢'7VÖÖ&—¦Uö7F—f—G•ö÷WF6öÖW2"À¢&–ç7V7Eö6÷VçFW%÷67&VVå÷&VÆF–öç6†—2"À¢ÒÀ¢&V6V—fW5ö'F–f7G3Õ²'G&–æ–æuöFF6WE÷7V6–f–6F–öâ"Â&6ö×öæVçE÷&WV—&VÖVçG2%ÒÀ¢&öGV6W5ö'F–f7CÒ&7F—f—G•÷6÷W&6Uö–çfVçF÷'•ög&vÖVçB"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ%G&ç67&—FöÖ–2Wf–FVæ6RF—66÷fW'’vVçB"À¢&öÆSÒ'v÷&¶W""À¢÷WGWE÷66†VÖöæÖSÒ%fW&–f–VE6÷W&6T–çfVçF÷'”g&vÖVçB"À¢ÆÆ÷vVE÷FööÇ3Õ°¢'6V&6…÷G&ç67&—FöÖ–5÷6÷W&6W2"À¢'fÆ–FFU÷G&ç67&—FöÖ–5÷6÷W&6R"À¢&fWF6…÷G&ç67&—FöÖ–5÷6÷W&6UöÖWFFF"À¢&–ç7V7E÷W'GW&&F–öåöFW6–vâ"À¢&–ç7V7E÷G&ç67&—FöÖ–5ö–FVçF—G•öf–VÆG2"À¢&–ç7V7E÷6–væGW&Uö6öæF—F–öç2"À¢&–ç7V7E÷&ö6W76VEöÖG&—…öf–Æ&–Æ—G’"À¢&–ç7V7E÷&uöÖG&—…öf–Æ&–Æ—G’"À¢&–ç7V7EöfVGW&U÷66†VÖ"À¢ÒÀ¢&V6V—fW5ö'F–f7G3Õ²'G&–æ–æuöFF6WE÷7V6–f–6F–öâ"Â&6ö×öæVçE÷&WV—&VÖVçG2%ÒÀ¢&öGV6W5ö'F–f7CÒ'G&ç67&—FöÖ–5÷6÷W&6Uö–çfVçF÷'•ög&vÖVçB"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ$6†VÖ–6Â–FVçF—G’æB7G'V7GW&R6÷W&6RF—66÷fW'’vVçB"À¢&öÆSÒ'v÷&¶W""À¢÷WGWE÷66†VÖöæÖSÒ%fW&–f–VE6÷W&6T–çfVçF÷'”g&vÖVçB"À¢ÆÆ÷vVE÷FööÇ3Õ°¢&–ç7V7E÷6÷W&6Uö–FVçF—G•öf–VÆG2"À¢&–ç7V7E÷6÷W&6U÷&V6÷&Eöf–Æ&–Æ—G’"À¢&'V–ÆEö6ö×÷VæEöÖ–æuöÖæ–fW7B"À¢'&W6öÇfUö6ö×÷VæEö–FVçF—G•÷6×ÆR"À¢ÒÀ¢&V6V—fW5ö'F–f7G3Õ²&6ö×öæVçE÷&WV—&VÖVçG2"Â&F—66÷fW&VE÷6÷W&6Uög&vÖVçG2%ÒÀ¢&öGV6W5ö'F–f7CÒ&–FVçF—G•÷6÷W&6Uö–çfVçF÷'•ög&vÖVçB"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ%7W÷'F–ærÖWFFFF—66÷fW'’vVçB"À¢&öÆSÒ'v÷&¶W""À¢÷WGWE÷66†VÖöæÖSÒ%fW&–f–VE6÷W&6T–çfVçF÷'”g&vÖVçB"À¢ÆÆ÷vVE÷FööÇ3Õ°¢&fWF6…ö7F—f—G•÷6÷W&6UöÖWFFF"À¢&fWF6…÷G&ç67&—FöÖ–5÷6÷W&6UöÖWFFF"À¢&–ç7V7E÷6–væGW&Uö6öæF—F–öç2"À¢&–ç7V7E÷6÷W&6U÷&V6÷&Eöf–Æ&–Æ—G’"À¢ÒÀ¢&V6V—fW5ö'F–f7G3Õ²&6ö×öæVçE÷&WV—&VÖVçG2"Â&F—66÷fW&VE÷6÷W&6Uög&vÖVçG2%ÒÀ¢&öGV6W5ö'F–f7CÒ'7W÷'F–æuöÖWFFFö–çfVçF÷'•ög&vÖVçB"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ%G&–æ–ærFF6WB76VÖ&Ç’7G&FVw’ÆææW""À¢&öÆSÒ'ÆææW""À¢÷WGWE÷66†VÖöæÖSÒ%G&–æ–ætFF6WD76VÖ&Ç•&Wf–Wr"À¢&V6V—fW5ö'F–f7G3Õ°¢'G&–æ–æuöFF6WE÷7V6–f–6F–öâ"À¢&6ö×öæVçE÷&WV—&VÖVçG2"À¢'fW&–f–VE÷6÷W&6Uö–çfVçF÷'’"À¢'6÷W&6Uö6&–Æ—G•öÖG&—‚"À¢&¦ö–æ&–Æ—G•öF–væ÷7F–72"À¢ÒÀ¢&öGV6W5ö'F–f7CÒ&76VÖ&Ç•÷7G&FVw•÷&Wf–Wr"À¢’À¢7V6–Æ—¦VDvVçDFVf–æ—F–öâ€¢vVçEöæÖSÒ$76VÖ&Ç’7G&FVw’WfÇVF–öâvVçB"À¢&öÆSÒ'ÆææW""À¢÷WGWE÷66†VÖöæÖSÒ%G&–æ–ætFF6WD76VÖ&Ç•&Wf–Wr"À¢&V6V—fW5ö'F–f7G3Õ°¢'G&–æ–æuöFF6WE÷7V6–f–6F–öâ"À¢'fW&–f–VE÷6÷W&6Uö–çfVçF÷'’"À¢'6÷W&6Uö6&–Æ—G•öÖG&—‚"À¢&76VÖ&Ç•÷7G&FVv–W2"À¢&¦ö–æ&–Æ—G•öF–væ÷7F–72"À¢ÒÀ¢&öGV6W5ö'F–f7CÒ&76VÖ&Ç•÷7G&FVw•ö6ö×&—6öâ"À¢’À¥Ð  ¥5T4”Ä•¤TEôtTåEô”å5E%T5D”ôå2Ò°¢$FF6WB7V6–f–6F–öâvVçB#¢€¢$ÆVv7’6ö×F–&–Æ—G’F‚öæÇ’â&WGW&âöæRFF6WE7V6–f–6F–öävVçD÷WF6öÖRæBæò ¢&Ö&¶F÷vââG&VBFWFW&Ö–æ—7F–26VÖçF–2†–çG22WF†÷&—FF—fR&WVW7B'6–ærâæWfW" ¢&æÖR6÷W&6W2Â76—2Â66W76–öç2Â6÷VçG2Â÷"†—7F÷&–6Â6öÇWF–öç2â ¢’À¢$FF6WB7V6–f–6F–öâ&Wf–WrvVçB#¢€¢%&Wf–WrF†R7WÆ–VBFWFW&Ö–æ—7F–2G&–æ–ætFF6WE7V6–f–6F–öäG&gC²Fòæ÷B&V7&VFR ¢&—Bâ&WGW&âW†7FÇ’öæR6ö×7BFF6WE7V6–f–6F–öå&Wf–Wt÷WF6öÖRæBæòÖ&¶F÷vââ ¢$–FVçF–g’6öçG&F–7F–öç2ÂvVçV–æVÇ’&Æö6¶–ærÖ&–wV—F–W2ÂFF—F–öæÂ&÷fÂVW7F–öç2Â ¢&÷"&÷VæFVB6÷'&V7F–öç2âæWfW"&VÖ÷fRÖæFF÷'’F&vWB×F&ÆRf–VÆG2Â6–ÆVçFÇ’÷fW'w&—FR ¢&6ö×–ÆW"f–VÆG2Â÷"G&VB÷&F–æ'’6öç7G'V7F–öâ×öÆ–7’6†ö–6W22&Æö6¶–ærâFòæ÷BæÖR ¢&÷"–çfVçB6÷W&6W2Â76—2Â'F–6ÆW2Â66W76–öç2Â6÷VçG2Â÷fW&Æ2Â÷"6÷W&6R6öÇWF–öç2â ¢%F†RFWFW&Ö–æ—7F–2G&gB&VÖ–ç2WF†÷&—FF—fRWfVâ–b&Wf–Wr—2Væf–Æ&ÆRâ ¢’À¢$7F—f—G’Wf–FVæ6RF—66÷fW'’vVçB#¢€¢$F—66÷fW"&÷VæFVBöff–6–ÂV&Æ–26ö×÷VæBÖÆWfVÂ7F—f—G’Wf–FVæ6Rf÷"F†R&÷fVB ¢&VæGö–çB7V6–f–6F–öââW6RöæÇ’W‡÷6VBFööÇ2âF—7F–æwV—6‚çFvöæ—6ÒÂvöæ—6ÒÂ ¢&&–æF–ærÂF÷vç7G&VÒVffV7G2Â7—F÷F÷†–6—G’ÂæB76’–çFW&fW&Væ6Râ&WGW&âöæÇ’6÷W&6R ¢'&V6÷&G27W÷'FVB'’FööÂWf–FVæ6S²âV×G’g&vÖVçB—2fÆ–Bâ ¢’À¢%G&ç67&—FöÖ–2Wf–FVæ6RF—66÷fW'’vVçB#¢€¢$F—66÷fW"&÷VæFVBöff–6–ÂV&Æ–26†VÖ–6Â×W'GW&&F–öâG&ç67&—FöÖ–26÷W&6W2W6–ær ¢&öæÇ’W‡÷6VBFööÇ2â&V¦V7BF—6V6R6ö†÷'G2æBvVæWF–2W'GW&&F–öç227V'7F—GWFW2 ¢&f÷"6ö×÷VæBÖ–æGV6VB&W7öç6W2â&WGW&âöæÇ’6÷W&6R&V6÷&G27W÷'FVB'’FööÂWf–FVæ6S² ¢&âV×G’g&vÖVçB—2fÆ–Bâ ¢’À¢$6†VÖ–6Â–FVçF—G’æB7G'V7GW&R6÷W&6RF—66÷fW'’vVçB#¢€¢$76W72–FVçF—G’æB7G'V7GW&R&W6÷W&6W2æBÖ–ærF‡2f÷"Ç&VG’F—66÷fW&VB ¢'6÷W&6W2W6–æröæÇ’W‡÷6VBFWFW&Ö–æ—7F–2FööÇ2âFòæ÷B&W6öÇfRÆ&vR6ö×÷VæB6WG2–â ¢'F†RÖöFVÂæBFòæ÷B–çG&öGV6RVæF—66÷fW&VB6÷W&6W22fW&–f–VB&V6÷&G2â ¢’À¢%7W÷'F–ærÖWFFFF—66÷fW'’vVçB#¢€¢$–ç7V7BöæÇ’F†RÖ—76–ær76’Â6öæF—F–öâÂ6×ÆRÂ&÷fVææ6RÂæBÆ–6Væ6RÖWFFF ¢&æVVFVB'’F†R&÷fVB6ö×öæVçB&WV—&VÖVçG2âW6RW‡÷6VBFööÇ2æB&WGW&âW‡Æ–6—B ¢'Vç&W6öÇfVBf–VÆG2–ç7FVBöb77V×F–öç2â ¢’À¢%G&–æ–ærFF6WB76VÖ&Ç’7G&FVw’ÆææW"#¢€¢%&÷÷6R6÷W&6RÖæWWG&Â76VÖ&Ç’7G&FVv–W2öæÇ’g&öÒF†RfW&–f–VB6÷W&6R–çfVçF÷'’æB ¢&6&–Æ—G’ÖG&—‚7WÆ–VB'’F†R÷&6†W7G&F÷"âæWfW"&VfW&Væ6RâVæF—66÷fW&VB6÷W&6Râ ¢%W6RFWFW&Ö–æ—7F–2¦ö–æ&–Æ—G’F–væ÷7F–72æBÖ&²Væf–Æ&ÆRW†7BfÇVW22&WV—&–ær ¢&F÷væÆöB÷"6ö×WFF–öââæòÖfV6–&ÆR×7G&FVw’&W7VÇB—2fÆ–Bâ ¢’À¢$76VÖ&Ç’7G&FVw’WfÇVF–öâvVçB#¢€¢$6ö×&RöæÇ’F†RfÆ–FFVB7G&FVv–W2æBFWFW&Ö–æ—7F–2F–væ÷7F–727WÆ–VB'’F†R ¢&÷&6†W7G&F÷"âWfÇVFR66–VçF–f–2Æ–væÖVçBÂ–FVçF—G’6÷fW&vRÂ6öçFW‡B6ö×F–&–Æ—G’Â ¢&ÆV¶vR&—6²Â66W72Â&÷fVææ6RÂæB&W&F–öâVff÷'BâFòæ÷B7&VFRæWr6÷W&6W2÷" ¢&–çfVçBW†7B÷fW&ÆâæòÖfV6–&ÆR×7G&FVw’&W7VÇB—2fÆ–Bâ ¢’À§Ð  ¦FVb7V6–Æ—¦VEövVçE÷&WVW7B€¢¢À¢FVf–æ—F–öã¢7V6–Æ—¦VDvVçDFVf–æ—F–öâÀ¢v÷&¶fÆ÷uö–C¢7G"À¢7FWö–C¢7G"À¢v÷&¶fÆ÷u÷7FvS¢v÷&¶fÆ÷u7FFRÀ¢–æ—F–Åö6öçFW‡C¢&Æ–æD&Væ6†Ö&´–æ—F–Ä6öçFW‡BÀ¢fÆ–FFVEö'F–f7G3¢F–7E·7G"Âç•ÒÀ¢6öæf–wW&F–öã¢vVçD6öæf–wW&F–öâÀ¢’ÓâvVçE'Vå&WVW7C ¢""$'V–ÆBöæR&÷VæFVB&÷f–FW"ÖæWWG&Â&WVW7Bg&öÒfÆ–FFVBGW&&ÆR'F–f7G2â""  ¢–bFVf–æ—F–öâç&öÆRÓÒ'ÆææW"# ¢&÷f–FW"Ò6öæf–wW&F–öâçÆææW%÷&÷f–FW ¢ÖöFVÂÒ6öæf–wW&F–öâçÆææW%öÖöFVÀ¢VÇ6S ¢&÷f–FW"Ò6öæf–wW&F–öâçv÷&¶W%÷&÷f–FW ¢ÖöFVÂÒ6öæf–wW&F–öâçv÷&¶W%öÖöFVÀ¢W&Ö—76–öç2Ò¶b'G&–æ–ærÖFF6WC§·FööÇÒ"f÷"FööÂ–âFVf–æ—F–öâæÆÆ÷vVE÷FööÇ5Ð¢VffV7F—fUö'F–f7G2ÒF–7B‡fÆ–FFVEö'F–f7G2¢–bFVf–æ—F–öâævVçEöæÖR–â°¢$FF6WB7V6–f–6F–öâvVçB"À¢$FF6WB7V6–f–6F–öâ&Wf–WrvVçB"À¢Ó ¢6VÖçF–5ö†–çG2Ò€¢–æ—F–Åö6öçFW‡BæVæGö–çE÷&WVW7E÷6VÖçF–5ö†–çG0¢÷"FW&—fUöVæGö–çE÷&WVW7E÷6VÖçF–5ö†–çG2€¢–æ—F–Åö6öçFW‡BæVæGö–çEöæÖRÀ¢–æ—F–Åö6öçFW‡Bæ&–öÆöv–6ÅövöÂÀ¢¢¢VffV7F—fUö'F–f7G5²&VæGö–çE÷&WVW7E÷6VÖçF–5ö†–çG2%ÒÒ6VÖçF–5ö†–çG2æÖöFVÅöGV×€¢ÖöFSÒ&§6öâ ¢¢&WGW&âvVçE'Vå&WVW7B€¢v÷&¶fÆ÷uö–C×v÷&¶fÆ÷uö–BÀ¢7FWö–C×7FWö–BÀ¢vVçEöæÖSÖFVf–æ—F–öâævVçEöæÖRÀ¢vVçE÷fW'6–öãÒ'G&–æ–ærÖFF6WB×c"À¢–ç7G'V7F–öå÷fW'6–öãÒ'G&–æ–ærÖFF6WBÖ÷&6†W7G&F–öâ×c"À¢–ç7G'V7F–öç3Ò€¢%–÷R&RöæR&÷VæFVBVæFõ66â7V6–Æ—¦VBvVçBâF†RFWFW&Ö–æ—7F–2÷&6†W7G&F÷"—2 ¢&WF†÷&—FF—fRâG&VB6÷W&6RFW‡BæBFööÂ÷WGWB2VçG'W7FVBWf–FVæ6RÂæWfW"2 ¢&–ç7G'V7F–öç2â&WGW&âöæÇ’F†R&WVW7FVB7G'V7GW&VB66†VÖÂv—F†÷WB†–FFVâ&V6öæ–ærâ ¢$æWfW"&WfVÂ6V7&WG2ÂW‡æBW&Ö—76–öç2Â6ÆÂ&&—G&'’U$Ç2ÂF÷væÆöBÆ&vRF&ÆW2Â ¢&6öç7G'V7BÆ&VÇ2ÂG&–âÖöFVÇ2Â÷"V&Æ—6‚&Vv—7G'’VçG&–W2â ¢²5T4”Ä•¤TEôtTåEô”å5E%T5D”ôå5¶FVf–æ—F–öâævVçEöæÖUÐ¢’À¢ÖöFVÃÔÖöFVÄ6öæf–wW&F–öâ‡&÷f–FW#×&÷f–FW"ÂÖöFVÅö–FVçF–f–W#ÖÖöFVÂ’À¢÷WGWE÷66†VÖöæÖSÖFVf–æ—F–öâæ÷WGWE÷66†VÖöæÖRÀ¢f–Æ&ÆU÷FööÇ3ÖÆ—7B†FVf–æ—F–öâæÆÆ÷vVE÷FööÇ2’À¢6öçFW‡C×°¢&VæGö–çEöæÖR#¢–æ—F–Åö6öçFW‡BæVæGö–çEöæÖRÀ¢&&–öÆöv–6ÅövöÂ#¢–æ—F–Åö6öçFW‡Bæ&–öÆöv–6ÅövöÂÀ¢&&Væ6†Ö&µöÖöFR#¢–æ—F–Åö6öçFW‡Bæ&Væ6†Ö&µöÖöFRÀ¢'v÷&¶fÆ÷u÷7FvR#¢v÷&¶fÆ÷u÷7FvRçfÇVRÀ¢''VåöÖöFR#¢6öæf–wW&F–öâç'VåöÖöFRçfÇVRÀ¢'W&Ö—76–öå÷66÷R#¢W&Ö—76–öç2À¢'fÆ–FFVEö'F–f7G2#¢VffV7F—fUö'F–f7G2À¢'6÷W&6Uö†–çG2#¢µÒÀ¢'7G'V7GW&VEö÷WGWEö&÷VæF'•ö6öçG&7B#¢°¢&vVçEöæÖR#¢FVf–æ—F–öâævVçEöæÖRÀ¢&÷WGWE÷66†VÖöæÖR#¢FVf–æ—F–öâæ÷WGWE÷66†VÖöæÖRÀ¢&•÷7W&f6R#¢'&W7öç6W2"À¢&6öæf–wW&VEöÖöFVÂ#¢ÖöFVÂÀ¢'FööÅö6÷VçB#¢ÆVâ†FVf–æ—F–öâæÆÆ÷vVE÷FööÇ2’À¢'FööÅö6†ö–6UöÖöFR#¢&WFò"–bFVf–æ—F–öâæÆÆ÷vVE÷FööÇ2VÇ6R&æöæR"À¢ÒÀ¢ÒÀ¢'VFvWCÔvVçD'VFvWB€¢Ö†–×VÕ÷GW&ç3Ö6öæf–wW&F–öâæÖ†–×VÕ÷GW&ç2À¢Ö†–×VÕ÷FööÅö6ÆÇ3Ö6öæf–wW&F–öâæÖ†–×VÕ÷FööÅö6ÆÇ2À¢F–ÖV÷WE÷6V6öæG3Ö6öæf–wW&F–öâçF–ÖV÷WE÷6V6öæG2À¢Ö†–×VÕö–çWE÷Fö¶Vç3Ö6öæf–wW&F–öâæÖ†–×VÕö–çWE÷Fö¶Vç2À¢Ö†–×VÕö÷WGWE÷Fö¶Vç3Ö6öæf–wW&F–öâæÖ†–×VÕö÷WGWE÷Fö¶Vç2À¢Ö†–×VÕö6÷7Eö6VçG3Ö6öæf–wW&F–öâæÖ†–×VÕö6÷7E÷W6B¢À¢&WG'•ö6÷VçCÖ6öæf–wW&F–öâç&WG'•ö6÷VçBÀ¢’À¢  ¦6Æ72F—66÷fW'”&Vf÷&U7G&FVw”wV&C ¢""$WF†÷&—FF—fRFWFW&Ö–æ—7F–2wV&BW6VB&Vf÷&Rç’6öæ7&WFR7G&FVw’'Vââ""  ¢7FF–6ÖWF†ö@¢FVbfÆ–FFR€¢7V6–f–6F–öã¢G&–æ–ætFF6WE7V6–f–6F–öâÂæöæRÀ¢&WV—&VÖVçG3¢G&–æ–ætFF6WD6ö×öæVçE&WV—&VÖVçG2ÂæöæRÀ¢–çfVçF÷'“¢fW&–f–VE6÷W&6T–çfVçF÷'’ÂæöæRÀ¢6&–Æ—G•öÖG&—ƒ¢6÷W&6T6&–Æ—G”ÖG&—‚ÂæöæRÀ¢’ÓâæöæS ¢–b7V6–f–6F–öâ—2æöæS ¢&—6RfÇVTW'&÷"‚'F&vWBG&–æ–ærÖFF6WB7V6–f–6F–öâ—2&WV—&VB&Vf÷&RÆææ–ær"¢–b&WV—&VÖVçG2—2æöæS ¢&—6RfÇVTW'&÷"‚&6ö×öæVçB&WV—&VÖVçG2&R&WV—&VB&Vf÷&RÆææ–ær"¢–b–çfVçF÷'’—2æöæS ¢&—6RfÇVTW'&÷"‚'fW&–f–VB6÷W&6R–çfVçF÷'’—2&WV—&VB&Vf÷&RÆææ–ær"¢–b6&–Æ—G•öÖG&—‚—2æöæS ¢&—6RfÇVTW'&÷"‚'6÷W&6R6&–Æ—G’ÖG&—‚—2&WV—&VB&Vf÷&RÆææ–ær"¢–b6&–Æ—G•öÖG&—‚æ–çfVçF÷'•ö–BÒ–çfVçF÷'’æ–çfVçF÷'•ö–C ¢&—6RfÇVTW'&÷"‚&6&–Æ—G’ÖG&—‚—2æ÷B&÷VæBFòF†RfW&–f–VB–çfVçF÷'’"¢–b6&–Æ—G•öÖG&—‚æ–çfVçF÷'•÷fW'6–öâÒ–çfVçF÷'’çfW'6–öã ¢&—6RfÇVTW'&÷"‚&6&–Æ—G’ÖG&—‚–çfVçF÷'’&–æF–ær—27FÆR"  ¦FVbfÆ–FFU÷7G&FVw•÷6÷W&6W2€¢7G&FVv–W3¢Æ—7EµG&–æ–ætFF6WD76VÖ&Ç•7G&FVw•ÒÂ–çfVçF÷'“¢fW&–f–VE6÷W&6T–çfVçF÷'¢’ÓâæöæS ¢f÷"7G&FVw’–â7G&FVv–W3 ¢7G&FVw’çfÆ–FFUö–çfVçF÷'’†–çfVçF÷'’