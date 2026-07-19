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
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    validation_status: SourceValidationStatus
    artifact_hashes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def roles_match_capabilities(self) -> VerifiedSourceRecord:
        capability_roles = {item.component for item in self.capabilities}
        if not set(self.source_roles).issubset(capability_roles):
            raise ValueError("every source role must have a capability record")
        return self


class VerifiedSourceInventory(StrictContract):
    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    inventory_id: str = Field(min_length=3, max_length=160)
    version: int = Field(ge=1)
    specification_id: str
    sources: list[VerifiedSourceRecord] = Field(default_factory=list, max_length=500)
    discovery_complete_for_roles: list[ComponentRole] = Field(default_factory=list, max_length=50)
    missing_roles: list[ComponentRole] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_sources(self) -> VerifiedSourceInventory:
        ids = [item.source_id for item in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("verified source IDs must be unique")
        return self

    @property
    def source_ids(self) -> set[str]:
        return {item.source_id for item in self.sources}


class VerifiedSourceInventoryFragment(StrictContract):
    """Bounded agent output; validation and merging remain deterministic."""

    fragment_id: str = Field(min_length=3, max_length=160)
    component_roles: list[ComponentRole] = Field(min_length=1, max_length=20)
    candidate_records: list[VerifiedSourceRecord] = Field(default_factory=list, max_length=100)
    evidence_used: list[str] = Field(default_factory=list, max_length=200)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)


class CapabilityMatrixCell(StrictContract):
    source_id: str
    component: ComponentRole
    status: CapabilityStatus
    fields: list[str] = Field(default_factory=list, max_length=100)
    evidence_references: list[str] = Field(default_factory=list, max_length=100)


class SourceCapabilityMatrix(StrictContract):
    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    inventory_id: str
    inventory_version: int = Field(ge=1)
    components: list[ComponentRole]
    cells: list[CapabilityMatrixCell] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def unique_cells(self) -> SourceCapabilityMatrix:
        keys = [(item.source_id, item.component) for item in self.cells]
        if len(keys) != len(set(keys)):
            raise ValueError("capability matrix cells must be unique")
        return self


def build_capability_matrix(
    inventory: VerifiedSourceInventory,
    requirements: TrainingDatasetComponentRequirements,
) -> SourceCapabilityMatrix:
    components = list(dict.fromkeys(item.role for item in requirements.requirements))
    cells: list[CapabilityMatrixCell] = []
    for source in inventory.sources:
        by_role = {item.component: item for item in source.capabilities}
        for component in components:
            capability = by_role.get(component)
            cells.append(
                CapabilityMatrixCell(
                    source_id=source.source_id,
                    component=component,
                    status=capability.status if capability else CapabilityStatus.UNAVAILABLE,
                    fields=capability.fields if capability else [],
                    evidence_references=capability.evidence_references if capability else [],
                )
            )
    return SourceCapabilityMatrix(
        inventory_id=inventory.inventory_id,
        inventory_version=inventory.version,
        components=components,
        cells=cells,
    )


class AssemblyGraphNode(StrictContract):
    node_id: str = Field(min_length=1, max_length=160)
    node_type: GraphNodeType
    label: str = Field(min_length=1, max_length=500)
    source_id: str | None = Field(default=None, max_length=160)
    source_roles: list[ComponentRole] = Field(default_factory=list, max_length=30)
    produces_fields: list[str] = Field(default_factory=list, max_length=200)
    operation: str | None = Field(default=None, max_length=2000)
    unresolved_gaps: list[str] = Field(default_factory=list, max_length=100)
    human_decisions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def source_node_requires_source(self) -> AssemblyGraphNode:
        if self.node_type is GraphNodeType.SOURCE and not self.source_id:
            raise ValueError("source graph nodes require source_id")
        if self.node_type is not GraphNodeType.SOURCE and self.source_id:
            raise ValueError("only source graph nodes may reference source_id")
        return self


class AssemblyGraphEdge(StrictContract):
    from_node: str
    to_node: str
    transferred_fields: list[str] = Field(default_factory=list, max_length=200)
    join_keys: list[str] = Field(default_factory=list, max_length=50)
    condition: str | None = Field(default=None, max_length=1000)


class TrainingDatasetAssemblyGraph(StrictContract):
    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    graph_id: str = Field(min_length=3, max_length=160)
    inventory_id: str
    inventory_version: int = Field(ge=1)
    target_specification_id: str
    target_fields: list[str] = Field(min_length=1, max_length=200)
    nodes: list[AssemblyGraphNode] = Field(min_length=1, max_length=1000)
    edges: list[AssemblyGraphEdge] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_graph(self) -> TrainingDatasetAssemblyGraph:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("assembly graph node IDs must be unique")
        known = set(ids)
        invalid_edges = [
            edge for edge in self.edges if edge.from_node not in known or edge.to_node not in known
        ]
        if invalid_edges:
            raise ValueError("assembly graph edges must reference existing nodes")
        indegree = dict.fromkeys(ids, 0)
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge.from_node].append(edge.to_node)
            indegree[edge.to_node] += 1
        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(ids):
            raise ValueError("assembly graph must be acyclic")
        terminal = [
            node for node in self.nodes if node.node_type is GraphNodeType.FINAL_CANDIDATE_TABLE
        ]
        if len(terminal) != 1:
            raise ValueError("assembly graph must contain exactly one final candidate-table node")
        missing = sorted(set(self.target_fields) - set(terminal[0].produces_fields))
        if missing:
            raise ValueError(
                f"final candidate table does not cover target fields: {', '.join(missing)}"
            )
        return self

    def validate_inventory(self, inventory: VerifiedSourceInventory) -> None:
        if (
            self.inventory_id != inventory.inventory_id
            or self.inventory_version != inventory.version
        ):
            raise ValueError("assembly graph inventory binding is stale")
        references = {node.source_id for node in self.nodes if node.source_id}
        undiscovered = sorted(references - inventory.source_ids)
        if undiscovered:
            raise ValueError(
                f"assembly graph references undiscovered sources: {', '.join(undiscovered)}"
            )


class JoinabilityDiagnostic(StrictContract):
    diagnostic_id: str = Field(min_length=3, max_length=160)
    status: JoinabilityStatus
    source_ids: list[str] = Field(min_length=1, max_length=100)
    identifier_type: str | None = Field(default=None, max_length=160)
    exact_overlap_count: int | None = Field(default=None, ge=0)
    partial_overlap_count: int | None = Field(default=None, ge=0)
    activity_coverage: float | None = Field(default=None, ge=0, le=1)
    transcriptomic_coverage: float | None = Field(default=None, ge=0, le=1)
    structure_coverage: float | None = Field(default=None, ge=0, le=1)
    class_counts: dict[str, int] = Field(default_factory=dict)
    feature_schema_compatible: bool | None = None
    condition_compatibility: str | None = Field(default=None, max_length=2000)
    required_downloads: list[str] = Field(default_factory=list, max_length=100)
    required_computations: list[str] = Field(default_factory=list, max_length=100)
    evidence_references: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def exact_values_require_exact_status(self) -> JoinabilityDiagnostic:
        if (
            self.status is not JoinabilityStatus.COMPUTED_EXACT
            and self.exact_overlap_count is not None
        ):
            raise ValueError("exact overlap may be reported only for computed_exact diagnostics")
        if self.status is JoinabilityStatus.COMPUTED_EXACT and self.exact_overlap_count is None:
            raise ValueError("computed_exact diagnostics require exact_overlap_count")
        return self


class TrainingDatasetPreparationStep(StrictContract):
    step_id: str = Field(min_length=2, max_length=160)
    order: int = Field(ge=1)
    action: str = Field(min_length=3, max_length=2000)
    status: PreparationStepStatus
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    target_fields: list[str] = Field(default_factory=list, max_length=100)
    evidence_references: list[str] = Field(default_factory=list, max_length=100)
    blocker: str | None = Field(default=None, max_length=2000)


class TrainingDatasetPreparationPlan(StrictContract):
    plan_id: str = Field(min_length=3, max_length=160)
    strategy_id: str
    steps: list[TrainingDatasetPreparationStep] = Field(min_length=1, max_length=200)
    requires_human_approval_before_training: bool = True

    @model_validator(mode="after")
    def ordered_steps(self) -> TrainingDatasetPreparationPlan:
        orders = [item.order for item in self.steps]
        if len(orders) != len(set(orders)) or sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("preparation steps must have unique contiguous order values")
        return self


class TrainingDatasetAssemblyStrategy(StrictContract):
    contract_version: Literal["1.0.0"] = TRAINING_DATASET_CONTRACT_VERSION
    strategy_id: str = Field(min_length=3, max_length=160)
    target_specification_id: str
    source_inventory_id: str
    source_inventory_version: int = Field(ge=1)
    source_graph: TrainingDatasetAssemblyGraph
    source_roles: dict[str, list[ComponentRole]]
    transformations: list[str] = Field(default_factory=list, max_length=200)
    joins: list[str] = Field(default_factory=list, max_length=200)
    identity_policy: str = Field(min_length=3, max_length=4000)
    chemical_standardization_policy: str = Field(min_length=3, max_length=4000)
    label_policy: str = Field(min_length=3, max_length=4000)
    transcriptomic_condition_policy: str = Field(min_length=3, max_length=4000)
    repeated_signature_policy: str = Field(min_length=3, max_length=4000)
    expected_output_grain: str = Field(min_length=3, max_length=1000)
    overlap_diagnostic: JoinabilityDiagnostic
    expected_coverage: dict[str, float | int | str] = Field(default_factory=dict)
    expected_class_balance: dict[str, int | float | str] = Field(default_factory=dict)
    evidence_quality: str = Field(min_length=3, max_length=4000)
    computational_requirements: list[str] = Field(default_factory=list, max_length=100)
    preparation_effort: str = Field(min_length=1, max_length=1000)
    scientific_risks: list[str] = Field(default_factory=list, max_length=100)
    technical_risks: list[str] = Field(default_factory=list, max_length=100)
    licensing_access_risks: list[str] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    missing_components: list[ComponentRole] = Field(default_factory=list, max_length=50)
    targeted_follow_up_search_requests: list[str] = Field(default_factory=list, max_length=50)
    fallback_strategy_id: str | None = Field(default=None, max_length=160)
    preparation_plan: TrainingDatasetPreparationPlan
    status: StrategyStatus
    requires_human_review: bool = True

    def validate_inventory(self, inventory: VerifiedSourceInventory) -> None:
        if (
            self.source_inventory_id != inventory.inventory_id
            or self.source_inventory_version != inventory.version
        ):
            raise ValueError("assembly strategy inventory binding is stale")
        self.source_graph.validate_inventory(inventory)
        undiscovered = sorted(set(self.source_roles) - inventory.source_ids)
        if undiscovered:
            raise ValueError(f"strategy references undiscovered sources: {', '.join(undiscovered)}")


class AssemblyGap(StrictContract):
    gap_id: str = Field(min_length=3, max_length=160)
    component: ComponentRole | None = None
    description: str = Field(min_length=3, max_length=2000)
    blocking: bool
    targeted_search_request: str = Field(min_length=3, max_length=2000)
    prior_query_fingerprints: list[str] = Field(default_factory=list, max_length=100)


class AssemblyGapReport(StrictContract):
    report_id: str = Field(min_length=3, max_length=160)
    inventory_id: str
    inventory_version: int = Field(ge=1)
    gaps: list[AssemblyGap] = Field(default_factory=list, max_length=100)
    discovery_round: int = Field(ge=0)
    maximum_discovery_rounds: int = Field(ge=0, le=10)
    requires_human_scope_review: bool = False

    def next_queries(self, already_executed: set[str]) -> list[str]:
        if self.discovery_round >= self.maximum_discovery_rounds:
            return []
        return [
            item.targeted_search_request
            for item in self.gaps
            if item.targeted_search_request not in already_executed
        ]


class TrainingDatasetAssemblyReview(StrictContract):
    review_id: str = Field(min_length=3, max_length=160)
    specification_id: str
    inventory_id: str
    inventory_version: int = Field(ge=1)
    strategies: list[TrainingDatasetAssemblyStrategy] = Field(default_factory=list, max_length=50)
    recommended_strategy_id: str | None = Field(default=None, max_length=160)
    decision_summary: str = Field(min_length=3, max_length=8000)
    evidence_used: list[str] = Field(default_factory=list, max_length=200)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    no_feasible_strategy: bool = False
    requires_human_review: bool = True

    @model_validator(mode="after")
    def validate_recommendation(self) -> TrainingDatasetAssemblyReview:
        ids = {item.strategy_id for item in self.strategies}
        if self.recommended_strategy_id and self.recommended_strategy_id not in ids:
            raise ValueError("recommended strategy must be included in strategies")
        if self.no_feasible_strategy and self.recommended_strategy_id:
            raise ValueError("no-feasible review cannot recommend a strategy")
        return self


class BlindBenchmarkInitialContext(StrictContract):
    benchmark_mode: str = Field(
        default=BLIND_TRAINING_DATASET_DISCOVERY, min_length=1, max_length=120
    )
    endpoint_name: str = Field(min_length=3, max_length=160)
    biological_goal: str = Field(min_length=10, max_length=4000)
    target_training_dataset_contract: dict[str, Any]
    source_adapter_capabilities: list[str] = Field(default_factory=list, max_length=100)
    approved_scientific_policies: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    planner_provider: str = Field(min_length=1, max_length=80)
    planner_model: str = Field(min_length=1, max_length=160)
    worker_provider: str = Field(min_length=1, max_length=80)
    worker_model: str = Field(min_length=1, max_length=160)
    budgets: dict[str, Any]
    source_hints: list[str] = Field(default_factory=list, max_length=0)
    article_hint: None = None
    doi_hint: None = None
    assay_id_hint: None = None
    activity_source_hint: None = None
    transcriptomic_source_hint: None = None
    expected_count_hint: None = None
    expected_overlap_hint: None = None
    endpoint_specific_mapping_hint: None = None

    @model_validator(mode="after")
    def forbid_source_hints(self) -> BlindBenchmarkInitialContext:
        if self.benchmark_mode != BLIND_TRAINING_DATASET_DISCOVERY:
            return self
        if self.source_hints:
            raise ValueError("blind benchmark context must not contain source hints")
        serialized = self.model_dump_json().casefold()
        forbidden = ("doi.org/", "pubmed", "bioassay id", "known accession")
        if any(token in serialized for token in forbidden):
            raise ValueError("blind benchmark context contains a prohibited source hint")
        return self


class SpecializedAgentDefinition(StrictContract):
    agent_name: str
    role: Literal["planner", "worker"]
    output_schema_name: str
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    receives_artifacts: list[str] = Field(default_factory=list, max_length=100)
    produces_artifact: str


SPECIALIZED_AGENT_SEQUENCE = [
    SpecializedAgentDefinition(
        agent_name="Dataset Specification Agent",
        role="planner",
        output_schema_name="TrainingDatasetSpecification",
        receives_artifacts=["endpoint_definition"],
        produces_artifact="training_dataset_specification",
    ),
    SpecializedAgentDefinition(
        agent_name="Activity Evidence Discovery Agent",
        role="worker",
        output_schema_name="VerifiedSourceInventoryFragment",
        allowed_tools=[
            "search_activity_sources",
            "validate_activity_source",
            "fetch_activity_source_metadata",
            "inspect_activity_result_availability",
            "inspect_activity_identifier_fields",
            "summarize_activity_outcomes",
            "inspect_counter_screen_relationships",
        ],
        receives_artifacts=["training_dataset_specification", "component_requirements"],
        produces_artifact="activity_source_inventory_fragment",
    ),
    SpecializedAgentDefinition(
        agent_name="Transcriptomic Evidence Discovery Agent",
        role="worker",
        output_schema_name="VerifiedSourceInventoryFragment",
        allowed_tools=[
            "search_transcriptomic_sources",
            "validate_transcriptomic_source",
            "fetch_transcriptomic_source_metadata",
            "inspect_perturbation_design",
            "inspect_transcriptomic_identity_fields",
            "inspect_signature_conditions",
            "inspect_processed_matrix_availability",
            "inspect_raw_matrix_availability",
            "inspect_feature_schema",
        ],
        receives_artifacts=["training_dataset_specification", "component_requirements"],
        produces_artifact="transcriptomic_source_inventory_fragment",
    ),
    SpecializedAgentDefinition(
        agent_name="Chemical Identity and Structure Source Discovery Agent",
        role="worker",
        output_schema_name="VerifiedSourceInventoryFragment",
        allowed_tools=[
            "inspect_source_identity_fields",
            "inspect_source_record_availability",
            "build_compound_mapping_manifest",
            "resolve_compound_identity_sample",
        ],
        receives_artifacts=["component_requirements", "discovered_source_fragments"],
        produces_artifact="identity_source_inventory_fragment",
    ),
    SpecializedAgentDefinition(
        agent_name="Supporting Metadata Discovery Agent",
        role="worker",
        output_schema_name="VerifiedSourceInventoryFragment",
        allowed_tools=[
            "fetch_activity_source_metadata",
            "fetch_transcriptomic_source_metadata",
            "inspect_signature_conditions",
            "inspect_source_record_availability",
        ],
        receives_artifacts=["component_requirements", "discovered_source_fragments"],
        produces_artifact="supporting_metadata_inventory_fragment",
    ),
    SpecializedAgentDefinition(
        agent_name="Training Dataset Assembly Strategy Planner",
        role="planner",
        output_schema_name="TrainingDatasetAssemblyReview",
        receives_artifacts=[
            "training_dataset_specification",
            "component_requirements",
            "verified_source_inventory",
            "source_capability_matrix",
            "joinability_diagnostics",
        ],
        produces_artifact="assembly_strategy_review",
    ),
    SpecializedAgentDefinition(
        agent_name="Assembly Strategy Evaluation Agent",
        role="planner",
        output_schema_name="TrainingDatasetAssemblyReview",
        receives_artifacts=[
            "training_dataset_specification",
            "verified_source_inventory",
            "source_capability_matrix",
            "assembly_strategies",
            "joinability_diagnostics",
        ],
        produces_artifact="assembly_strategy_comparison",
    ),
]


class DiscoveryBeforeStrategyGuard:
    """Authoritative deterministic guard used before any concrete strategy run."""

    @staticmethod
    def validate(
        specification: TrainingDatasetSpecification | None,
        requirements: TrainingDatasetComponentRequirements | None,
        inventory: VerifiedSourceInventory | None,
        capability_matrix: SourceCapabilityMatrix | None,
    ) -> None:
        if specification is None:
            raise ValueError("target training-dataset specification is required before planning")
        if requirements is None:
            raise ValueError("component requirements are required before planning")
        if inventory is None:
            raise ValueError("verified source inventory is required before planning")
        if capability_matrix is None:
            raise ValueError("source capability matrix is required before planning")
        if capability_matrix.inventory_id != inventory.inventory_id:
            raise ValueError("capability matrix is not bound to the verified inventory")
        if capability_matrix.inventory_version != inventory.version:
            raise ValueError("capability matrix inventory binding is stale")


def validate_strategy_sources(
    strategies: list[TrainingDatasetAssemblyStrategy], inventory: VerifiedSourceInventory
) -> None:
    for strategy in strategies:
        strategy.validate_inventory(inventory)
