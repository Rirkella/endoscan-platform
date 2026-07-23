"""Workflow-semantics v2 contracts for discovery, strategy, and recipe approval.

These contracts deliberately separate a search hit from a hydrated source, a
hydrated source from deterministic coverage, and a proposal from an approved
assembly recipe.  The orchestration layer may persist the documents and move
between states, but an LLM cannot remove ledger tasks or manufacture approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import StrictContract, utc_now

WORKFLOW_SEMANTICS_V2: Literal["2.0.0"] = "2.0.0"
ARTIFACT_CONTRACT_VERSION: Literal["2.0.0"] = "2.0.0"
OPERATIONAL_CAPABILITY_REGISTRY_VERSION: Literal["1.2.0"] = "1.2.0"


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_value(item) for item in value]
    return value


def _without_contract_versions(value: Any) -> Any:
    value = _json_value(value)
    if isinstance(value, dict):
        return {
            key: _without_contract_versions(item)
            for key, item in value.items()
            if key not in {"schema_version", "artifact_contract_version"}
        }
    if isinstance(value, list):
        return [_without_contract_versions(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _without_contract_versions(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def deterministic_fingerprint(value: Any) -> str:
    """Return the stable SHA-256 for a JSON-compatible scientific decision."""

    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ImmutableV2Contract(StrictContract):
    """Base for immutable v2 documents stored as content-addressed artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_contract_version: Literal["2.0.0"] = ARTIFACT_CONTRACT_VERSION


class ArtifactReference(ImmutableV2Contract):
    artifact_id: str = Field(min_length=3, max_length=160)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_type: str = Field(min_length=2, max_length=120)


class EvidenceRole(StrEnum):
    ACTIVITY = "activity"
    TRANSCRIPTOMIC = "transcriptomic"
    IDENTITY = "identity"
    SUPPORTING_METADATA = "supporting_metadata"


class OperationalCapability(StrEnum):
    CANDIDATE_SEARCH = "candidate_search"
    PAGINATION = "pagination"
    LIGHTWEIGHT_METADATA_CATALOGUE = "lightweight_metadata_catalogue"
    ACTIVITY_ASSAY_CATALOGUE = "activity_assay_catalogue"
    COMPOUND_IDENTIFIER_INDEX = "compound_identifier_index"
    ACTIVITY_CALL_RETRIEVAL = "activity_call_retrieval"
    RELATED_ASSAY_RELATIONSHIPS = "related_assay_relationships"
    PERTURBAGEN_CATALOGUE = "perturbagen_catalogue"
    SIGNATURE_METADATA_CATALOGUE = "signature_metadata_catalogue"
    CELL_METADATA_CATALOGUE = "cell_metadata_catalogue"
    GENE_METADATA_CATALOGUE = "gene_metadata_catalogue"
    DETERMINISTIC_COMPOUND_INDEX = "deterministic_compound_index"
    DETERMINISTIC_SIGNATURE_INDEX = "deterministic_signature_index"
    CONTEXT_COVERAGE = "context_coverage"
    CONTEXT_METADATA = "context_metadata"
    COMPOUND_SIGNATURE_OVERLAP = "compound_signature_overlap"
    PAIRWISE_COVERAGE = "pairwise_coverage"
    HEAVY_EXPRESSION_EXTRACTION = "heavy_expression_extraction"
    CACHE_REPLAY = "cache_replay"
    VERSION_CHECKSUM_PROVENANCE = "version_checksum_provenance"


class CapabilityAvailability(StrEnum):
    OPERATIONAL = "operational"
    PARTIAL = "partial"
    MISSING = "missing"
    POST_APPROVAL_ONLY = "post_approval_only"


class CapabilityPhase(StrEnum):
    PRE_APPROVAL = "pre_approval"
    POST_APPROVAL = "post_approval"


class ProviderCapabilityDeclaration(ImmutableV2Contract):
    capability: OperationalCapability
    availability: CapabilityAvailability
    phase: CapabilityPhase
    typed_tool_or_adapter_method: str | None = Field(default=None, max_length=160)
    input_contract: str | None = Field(default=None, max_length=160)
    output_contract: str | None = Field(default=None, max_length=160)
    evidence: str = Field(min_length=3, max_length=1000)

    @property
    def satisfies_preapproval_requirement(self) -> bool:
        return (
            self.phase is CapabilityPhase.PRE_APPROVAL
            and self.availability is CapabilityAvailability.OPERATIONAL
        )


class ProviderOperationalProfile(ImmutableV2Contract):
    provider: str = Field(min_length=2, max_length=120)
    display_name: str = Field(min_length=2, max_length=160)
    registered_source_id: str = Field(min_length=2, max_length=120)
    evidence_roles: list[EvidenceRole] = Field(min_length=1, max_length=10)
    supported_modalities: list[str] = Field(default_factory=list, max_length=50)
    capabilities: list[ProviderCapabilityDeclaration] = Field(min_length=1, max_length=50)
    generic_fallback_prohibited: Literal[True] = True

    @model_validator(mode="after")
    def unique_capabilities(self) -> ProviderOperationalProfile:
        names = [item.capability for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("provider capabilities must be unique")
        return self

    def declaration(self, capability: OperationalCapability) -> ProviderCapabilityDeclaration:
        for item in self.capabilities:
            if item.capability is capability:
                return item
        return ProviderCapabilityDeclaration(
            capability=capability,
            availability=CapabilityAvailability.MISSING,
            phase=CapabilityPhase.PRE_APPROVAL,
            evidence="Capability is not declared by the operational provider registry.",
        )


class OperationalCapabilityRegistry(ImmutableV2Contract):
    registry_version: Literal["1.2.0"] = OPERATIONAL_CAPABILITY_REGISTRY_VERSION
    providers: list[ProviderOperationalProfile] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_providers(self) -> OperationalCapabilityRegistry:
        names = [item.provider for item in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("operational provider IDs must be unique")
        return self

    def by_provider(self, provider: str) -> ProviderOperationalProfile:
        for item in self.providers:
            if item.provider == provider:
                return item
        raise KeyError(provider)


def load_operational_capability_registry(repo_root: Path) -> OperationalCapabilityRegistry:
    """Load the typed operational registry, which is separate from source approval."""

    path = Path(repo_root) / "registry" / "data" / "operational_provider_capabilities.json"
    return OperationalCapabilityRegistry.model_validate_json(path.read_text(encoding="utf-8"))


class PaginationCompletionPolicy(ImmutableV2Contract):
    maximum_pages: int = Field(ge=1, le=10_000)
    maximum_candidates: int | None = Field(default=None, ge=1)
    completion_conditions: list[str] = Field(min_length=1, max_length=20)
    top_n_is_completion: Literal[False] = False


class DiscoveryBudgets(ImmutableV2Contract):
    maximum_provider_invocations: int = Field(ge=0)
    maximum_tool_calls: int = Field(ge=0)
    maximum_scientific_source_requests: int = Field(ge=0)
    maximum_input_tokens: int = Field(ge=0)
    maximum_output_tokens: int = Field(ge=0)
    maximum_estimated_cost_usd: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    provider_retries: Literal[0] = 0


class ProviderDiscoveryTask(ImmutableV2Contract):
    task_id: str = Field(min_length=3, max_length=160)
    provider: str = Field(min_length=2, max_length=120)
    evidence_role: EvidenceRole
    modality: str | None = Field(default=None, max_length=120)
    operation: str = Field(min_length=2, max_length=160)
    typed_input_contract: str = Field(min_length=2, max_length=160)
    typed_output_contract: str = Field(min_length=2, max_length=160)
    query_parameters: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[OperationalCapability] = Field(min_length=1, max_length=30)
    pagination_policy: PaginationCompletionPolicy


class DiscoveryPlan(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    endpoint_identifier: str = Field(min_length=2, max_length=160)
    approved_endpoint_specification_artifact: ArtifactReference
    discovery_round: int = Field(ge=0)
    applicable_registered_providers: list[str] = Field(min_length=1, max_length=100)
    target_identifiers: list[str] = Field(min_length=1, max_length=50)
    target_synonyms: list[str] = Field(default_factory=list, max_length=100)
    requested_modalities: list[str] = Field(min_length=1, max_length=50)
    provider_specific_query_tasks: list[ProviderDiscoveryTask] = Field(min_length=1, max_length=500)
    required_hydration_fields: dict[EvidenceRole, list[str]]
    allowed_preapproval_tool_capabilities: list[OperationalCapability] = Field(
        min_length=1, max_length=50
    )
    scientific_and_execution_budgets: DiscoveryBudgets
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> DiscoveryPlan:
        task_ids = [item.task_id for item in self.provider_specific_query_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("discovery task IDs must be unique")
        if set(self.applicable_registered_providers) != {
            item.provider for item in self.provider_specific_query_tasks
        }:
            raise ValueError("applicable providers must exactly match planned tasks")
        payload = self.model_dump(mode="json", exclude={"plan_fingerprint"})
        if deterministic_fingerprint(payload) != self.plan_fingerprint:
            raise ValueError("discovery plan fingerprint does not match its content")
        return self

    @classmethod
    def create(cls, **values: Any) -> DiscoveryPlan:
        values["plan_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


class DiscoveryTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_NO_CANDIDATES = "completed_no_candidates"
    COMPLETED_WITH_PARTIAL_RESULTS = "completed_with_partial_results"
    FAILED = "failed"
    BLOCKED = "blocked"
    BLOCKED_PROVIDER_CAPABILITY_MISSING = "blocked_provider_capability_missing"


class DiscoveryExecutionRecord(ImmutableV2Contract):
    task_id: str = Field(min_length=3, max_length=160)
    provider: str = Field(min_length=2, max_length=120)
    evidence_role: EvidenceRole
    modality: str | None = Field(default=None, max_length=120)
    status: DiscoveryTaskStatus
    pages_or_cursors_attempted: list[str] = Field(default_factory=list, max_length=10_000)
    search_result_count: int = Field(default=0, ge=0)
    # Provider row accounting and compact workflow accounting are deliberately
    # separate.  A single assay/source candidate may summarize hundreds of
    # immutable provider rows without discarding any of those rows.
    raw_record_count: int = Field(default=0, ge=0)
    normalized_record_count: int = Field(default=0, ge=0)
    provider_unique_record_count: int = Field(default=0, ge=0)
    compact_source_candidate_count: int = Field(default=0, ge=0)
    retained_row_count: int = Field(default=0, ge=0)
    unique_candidate_count: int = Field(default=0, ge=0)
    source_response_artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=500)
    row_level_artifact_references: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )
    deterministic_summary_artifacts: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )
    completion_reason: str | None = Field(default=None, max_length=1000)
    error_classification: str | None = Field(default=None, max_length=160)
    retry_provenance: list[str] = Field(default_factory=list, max_length=20)
    logical_tool_call_count: int = Field(default=0, ge=0)
    scientific_source_request_count: int = Field(default=0, ge=0)
    transport_attempt_count: int = Field(default=0, ge=0)
    cache_hit_count: int = Field(default=0, ge=0)
    blocked_request_count: int = Field(default=0, ge=0)
    requests_prevented_by_cancellation: int = Field(default=0, ge=0)


class GlobalScientificSourceRequestAccounting(ImmutableV2Contract):
    allowed_global_request_budget: int = Field(ge=0)
    reserved_requests: int = Field(ge=0)
    completed_transport_attempts: int = Field(ge=0)
    failed_transport_attempts: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    blocked_requests_after_budget_exhaustion: int = Field(ge=0)
    requests_prevented_by_cancellation: int = Field(ge=0)
    remaining_requests: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_request_accounting(self) -> GlobalScientificSourceRequestAccounting:
        if self.reserved_requests > self.allowed_global_request_budget:
            raise ValueError("reserved scientific-source requests exceed the global budget")
        if self.remaining_requests != (self.allowed_global_request_budget - self.reserved_requests):
            raise ValueError("remaining scientific-source request budget is inconsistent")
        if (
            self.completed_transport_attempts + self.failed_transport_attempts
            > self.reserved_requests
        ):
            raise ValueError("terminal transport attempts exceed reserved requests")
        return self


class DiscoveryExecutionLedger(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    records: list[DiscoveryExecutionRecord] = Field(min_length=1, max_length=500)
    global_request_accounting: GlobalScientificSourceRequestAccounting | None = None

    @model_validator(mode="after")
    def unique_tasks(self) -> DiscoveryExecutionLedger:
        task_ids = [item.task_id for item in self.records]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("execution ledger task IDs must be unique")
        return self

    @property
    def complete(self) -> bool:
        terminal = {
            DiscoveryTaskStatus.COMPLETED,
            DiscoveryTaskStatus.COMPLETED_NO_CANDIDATES,
            DiscoveryTaskStatus.COMPLETED_WITH_PARTIAL_RESULTS,
            DiscoveryTaskStatus.FAILED,
            DiscoveryTaskStatus.BLOCKED,
            DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING,
        }
        return all(item.status in terminal for item in self.records)


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    METADATA_CANDIDATE = "metadata_candidate"
    DUPLICATE = "duplicate"
    EXCLUDED = "excluded"


class SourceCandidate(ImmutableV2Contract):
    candidate_id: str = Field(min_length=3, max_length=160)
    provider: str = Field(min_length=2, max_length=120)
    source_identifier: str = Field(min_length=1, max_length=500)
    evidence_role: EvidenceRole
    modality: str | None = Field(default=None, max_length=120)
    discovery_task_id: str = Field(min_length=3, max_length=160)
    contributing_task_ids: list[str] = Field(default_factory=list, max_length=500)
    contributing_modalities: list[str] = Field(default_factory=list, max_length=50)
    release_id: str | None = Field(default=None, max_length=160)
    source_version: str | None = Field(default=None, max_length=160)
    discovery_query_provenance: list[ArtifactReference] = Field(default_factory=list, max_length=50)
    provider_dataset_artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=50)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    relationship_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_status: CandidateStatus
    descriptive_rank: int | None = Field(default=None, ge=1)
    relevance_explanation: str | None = Field(default=None, max_length=2000)


class SourceCandidateSet(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[SourceCandidate] = Field(default_factory=list, max_length=20_000)
    all_planned_tasks_terminal: bool = False
    complete_without_failures: bool = False
    failed_task_ids: list[str] = Field(default_factory=list, max_length=500)
    partial_task_ids: list[str] = Field(default_factory=list, max_length=500)
    blocked_task_ids: list[str] = Field(default_factory=list, max_length=500)
    completed_task_ids: list[str] = Field(default_factory=list, max_length=500)
    completed_no_candidate_task_ids: list[str] = Field(default_factory=list, max_length=500)
    running_task_ids: list[str] = Field(default_factory=list, max_length=500)
    pending_task_ids: list[str] = Field(default_factory=list, max_length=500)
    missing_provider_modality_dimensions: list[str] = Field(default_factory=list, max_length=500)
    provider_dataset_artifacts: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )
    raw_record_count: int = Field(default=0, ge=0)
    normalized_record_count: int = Field(default=0, ge=0)
    provider_unique_record_count: int = Field(default=0, ge=0)
    compact_source_candidate_count: int = Field(default=0, ge=0)
    retained_row_count: int = Field(default=0, ge=0)
    accounting_invariant: Literal["provider_rows_retained_in_artifacts_and_summaries"] = (
        "provider_rows_retained_in_artifacts_and_summaries"
    )

    @model_validator(mode="after")
    def unique_candidates(self) -> SourceCandidateSet:
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("source candidate IDs must be unique")
        return self


class HydrationCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    SCIENTIFICALLY_UNUSABLE = "scientifically_unusable"


class HydratedSource(ImmutableV2Contract):
    hydrated_source_id: str = Field(min_length=3, max_length=160)
    source_candidate_id: str = Field(min_length=3, max_length=160)
    provider: str = Field(min_length=2, max_length=120)
    source_identifier: str = Field(min_length=1, max_length=500)
    evidence_role: EvidenceRole
    modality: str | None = Field(default=None, max_length=120)
    contributing_task_ids: list[str] = Field(default_factory=list, max_length=500)
    contributing_modalities: list[str] = Field(default_factory=list, max_length=50)
    release_id: str | None = Field(default=None, max_length=160)
    verified_metadata: dict[str, Any]
    compound_index_available: bool
    label_or_activity_fields_available: bool
    organism: str | None = Field(default=None, max_length=300)
    experimental_context: dict[str, Any] = Field(default_factory=dict)
    source_version: str | None = Field(default=None, max_length=160)
    licence_and_provenance: list[str] = Field(default_factory=list, max_length=100)
    completeness_status: HydrationCompleteness
    explicit_missing_fields: list[str] = Field(default_factory=list, max_length=100)
    exclusion_reason: str | None = Field(default=None, max_length=2000)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    relationship_summary: dict[str, Any] = Field(default_factory=dict)
    evidence_artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=500)


class HydratedSourceSet(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    sources: list[HydratedSource] = Field(default_factory=list, max_length=20_000)
    plan_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    all_candidates_have_outcomes: bool = False
    complete_without_failures: bool = False
    failed_task_ids: list[str] = Field(default_factory=list, max_length=500)
    partial_task_ids: list[str] = Field(default_factory=list, max_length=500)
    blocked_task_ids: list[str] = Field(default_factory=list, max_length=500)
    completed_task_ids: list[str] = Field(default_factory=list, max_length=500)
    completed_no_candidate_task_ids: list[str] = Field(default_factory=list, max_length=500)
    running_task_ids: list[str] = Field(default_factory=list, max_length=500)
    pending_task_ids: list[str] = Field(default_factory=list, max_length=500)
    missing_provider_modality_dimensions: list[str] = Field(default_factory=list, max_length=500)
    provider_dataset_artifacts: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )
    hydrated_source_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def unique_sources(self) -> HydratedSourceSet:
        ids = [item.hydrated_source_id for item in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("hydrated source IDs must be unique")
        if self.hydrated_source_count not in {0, len(ids)}:
            raise ValueError("hydrated-source accounting summary differs from retained sources")
        return self


class CombinationCoverage(ImmutableV2Contract):
    coverage_id: str = Field(min_length=3, max_length=160)
    activity_source_ids: list[str] = Field(min_length=1, max_length=100)
    transcriptomic_source_id: str = Field(min_length=3, max_length=160)
    requested_modality: str = Field(min_length=1, max_length=120)
    identity_resolution_count: int = Field(ge=0)
    activity_compound_count: int = Field(ge=0)
    transcriptomic_compound_count: int = Field(ge=0)
    overlap_count: int = Field(ge=0)
    active_count: int | None = Field(default=None, ge=0)
    inactive_count: int | None = Field(default=None, ge=0)
    ambiguous_count: int | None = Field(default=None, ge=0)
    metadata_completeness: dict[str, float] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    computation_provenance: list[ArtifactReference] = Field(default_factory=list, max_length=500)
    activity_provider: str | None = Field(default=None, max_length=120)
    transcriptomic_provider: str | None = Field(default=None, max_length=120)
    cell_or_tissue_context: list[str] = Field(default_factory=list, max_length=200)
    dose_values: list[str] = Field(default_factory=list, max_length=200)
    exposure_times: list[str] = Field(default_factory=list, max_length=200)
    identifier_resolution_policy: str = Field(default="stable_identifier_exact", max_length=160)
    stable_identifier_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    identifier_resolution_losses: int = Field(default=0, ge=0)
    activity_row_count: int = Field(default=0, ge=0)
    signature_profile_count: int = Field(default=0, ge=0)
    source_label_policy: str = Field(default="source_values_preserved", max_length=160)
    class_balance: dict[str, float] = Field(default_factory=dict)
    missing_metadata: dict[str, int] = Field(default_factory=dict)
    quality_flags: list[str] = Field(default_factory=list, max_length=100)
    conflicting_label_count: int = Field(default=0, ge=0)
    context_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_assembled_sample_count: int = Field(default=0, ge=0)
    expected_unique_compound_count: int = Field(default=0, ge=0)


class CombinationCoverageSet(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    combinations: list[CombinationCoverage] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def unique_combinations(self) -> CombinationCoverageSet:
        ids = [item.coverage_id for item in self.combinations]
        if len(ids) != len(set(ids)):
            raise ValueError("coverage IDs must be unique")
        return self


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    VIABLE = "viable"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class StrategyProposal(ImmutableV2Contract):
    proposal_id: str = Field(min_length=3, max_length=160)
    included_source_combination: list[str] = Field(min_length=2, max_length=200)
    modalities: list[str] = Field(min_length=1, max_length=50)
    proposed_context: dict[str, Any] = Field(default_factory=dict)
    proposed_label_policy: dict[str, Any]
    expected_dataset_size: int | None = Field(default=None, ge=0)
    expected_class_balance: dict[str, int] = Field(default_factory=dict)
    identifier_losses: int = Field(ge=0)
    metadata_losses: dict[str, int] = Field(default_factory=dict)
    scientific_strengths: list[str] = Field(default_factory=list, max_length=100)
    scientific_risks: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    provenance_references: list[ArtifactReference] = Field(default_factory=list, max_length=500)
    proposal_status: ProposalStatus
    expected_unique_compounds: int | None = Field(default=None, ge=0)
    context_rules: dict[str, Any] = Field(default_factory=dict)
    quality_constraints: list[str] = Field(default_factory=list, max_length=100)
    advantages: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    bias_risks: list[str] = Field(default_factory=list, max_length=100)
    required_postapproval_data_access: list[str] = Field(default_factory=list, max_length=100)


class StrategyProposalSet(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    proposals: list[StrategyProposal] = Field(default_factory=list, max_length=500)
    generation_basis: Literal["deterministic_coverage_artifacts"] = (
        "deterministic_coverage_artifacts"
    )

    @model_validator(mode="after")
    def unique_proposals(self) -> StrategyProposalSet:
        ids = [item.proposal_id for item in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("strategy proposal IDs must be unique")
        return self


class HumanApprovalRecord(ImmutableV2Contract):
    approval_id: str = Field(min_length=3, max_length=160)
    reviewer_id: str = Field(min_length=2, max_length=120)
    decision_event_id: str = Field(min_length=3, max_length=160)
    approved_at: datetime = Field(default_factory=utc_now)


class AssemblyRecipe(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    selected_strategy_proposal_id: str = Field(min_length=3, max_length=160)
    exact_source_versions: dict[str, str] = Field(min_length=1)
    exact_source_combination: list[str] = Field(min_length=2, max_length=200)
    approved_modalities: list[str] = Field(min_length=1, max_length=50)
    approved_modality_aggregation: dict[str, Any]
    approved_context_filters: dict[str, Any]
    approved_dose_time_rules: dict[str, Any]
    approved_label_policy: dict[str, Any]
    exclusion_rules: list[str] = Field(default_factory=list, max_length=200)
    required_extraction_fields: list[str] = Field(min_length=1, max_length=500)
    recipe_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    approver_action: HumanApprovalRecord
    created_at: datetime = Field(default_factory=utc_now)
    immutable_status: Literal["approved_immutable"] = "approved_immutable"
    endpoint_semantics_version: str = "2.0.0"
    approved_activity_sources: list[str] = Field(default_factory=list, max_length=100)
    approved_transcriptomic_sources: list[str] = Field(default_factory=list, max_length=100)
    approved_identity_mapping_policy: dict[str, Any] = Field(
        default_factory=lambda: {"operator": "stable_identifier_exact"}
    )
    approved_cell_tissue_contexts: list[str] = Field(default_factory=list, max_length=200)
    conflict_resolution_policy: dict[str, Any] = Field(
        default_factory=lambda: {"operator": "exclude_conflicts"}
    )
    replicate_aggregation_policy: dict[str, Any] = Field(
        default_factory=lambda: {"operator": "preserve_profiles"}
    )
    split_requirements: dict[str, Any] = Field(
        default_factory=lambda: {"group_by": "compound_id", "partitions": 3}
    )
    required_artifact_references: list[ArtifactReference] = Field(
        default_factory=list, max_length=500
    )

    @model_validator(mode="after")
    def validate_recipe_fingerprint(self) -> AssemblyRecipe:
        payload = self.model_dump(
            mode="json",
            exclude={
                "recipe_fingerprint",
                "approver_action",
                "created_at",
                "immutable_status",
            },
        )
        if deterministic_fingerprint(payload) != self.recipe_fingerprint:
            raise ValueError("assembly recipe fingerprint does not match its scientific content")
        return self

    @classmethod
    def create(cls, **values: Any) -> AssemblyRecipe:
        values.setdefault("endpoint_semantics_version", "2.0.0")
        values.setdefault("approved_activity_sources", [])
        values.setdefault("approved_transcriptomic_sources", [])
        values.setdefault(
            "approved_identity_mapping_policy", {"operator": "stable_identifier_exact"}
        )
        values.setdefault("approved_cell_tissue_contexts", [])
        values.setdefault("conflict_resolution_policy", {"operator": "exclude_conflicts"})
        values.setdefault("replicate_aggregation_policy", {"operator": "preserve_profiles"})
        values.setdefault("split_requirements", {"group_by": "compound_id", "partitions": 3})
        values.setdefault("required_artifact_references", [])
        scientific = {
            key: value
            for key, value in values.items()
            if key
            not in {
                "recipe_fingerprint",
                "approver_action",
                "created_at",
                "immutable_status",
            }
        }
        values["recipe_fingerprint"] = deterministic_fingerprint(scientific)
        return cls.model_validate(values)


class RegisteredProviderCapabilityMissing(ImmutableV2Contract):
    finding_type: Literal["REGISTERED_PROVIDER_CAPABILITY_MISSING"] = (
        "REGISTERED_PROVIDER_CAPABILITY_MISSING"
    )
    provider: str = Field(min_length=2, max_length=120)
    missing_capability: OperationalCapability
    missing_typed_tool_or_adapter_method: str
    required_input_contract: str
    required_output_contract: str
    blocked_workflow_stage: str
    current_fallback_behavior: Literal["none; generic fallback prohibited"] = (
        "none; generic fallback prohibited"
    )
    scientific_consequence: str = Field(min_length=10, max_length=2000)
    tests_required: list[str] = Field(min_length=1, max_length=30)


class ProviderCapabilityFindingSet(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    discovery_round: int = Field(ge=0)
    findings: list[RegisteredProviderCapabilityMissing] = Field(
        default_factory=list, max_length=500
    )


class DiscoveryRevisionRequest(ImmutableV2Contract):
    prior_plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_by: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=3, max_length=4000)
    provider_policy_revision: dict[str, Any] = Field(default_factory=dict)
    modality_policy_revision: dict[str, Any] = Field(default_factory=dict)
    context_constraint_revision: dict[str, Any] = Field(default_factory=dict)


class StrategySetRejection(ImmutableV2Contract):
    discovery_round: int = Field(ge=0)
    proposal_ids: list[str] = Field(min_length=1, max_length=500)
    rejected_by: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=3, max_length=4000)


def build_discovery_plan(
    *,
    workflow_id: str,
    endpoint_identifier: str,
    approved_specification_artifact: ArtifactReference,
    discovery_round: int,
    biological_target: str,
    target_synonyms: list[str],
    requested_modalities: list[str],
    registry: OperationalCapabilityRegistry,
    budgets: DiscoveryBudgets,
) -> DiscoveryPlan:
    """Compile every applicable provider task without source selection or fallback."""

    pagination = PaginationCompletionPolicy(
        maximum_pages=100,
        maximum_candidates=None,
        completion_conditions=[
            "provider cursor exhausted or provider explicitly declares end of catalogue",
            "all unique candidates are retained",
            "bounded interruption remains incomplete rather than completed",
        ],
    )
    role_contracts: dict[EvidenceRole, tuple[str, str, str, list[OperationalCapability]]] = {
        EvidenceRole.ACTIVITY: (
            "search_activity_catalogue",
            "ActivityCatalogueSearchInput",
            "ActivityCataloguePage",
            [
                OperationalCapability.CANDIDATE_SEARCH,
                OperationalCapability.PAGINATION,
                OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
                OperationalCapability.ACTIVITY_ASSAY_CATALOGUE,
                OperationalCapability.COMPOUND_IDENTIFIER_INDEX,
                OperationalCapability.ACTIVITY_CALL_RETRIEVAL,
                OperationalCapability.RELATED_ASSAY_RELATIONSHIPS,
            ],
        ),
        EvidenceRole.TRANSCRIPTOMIC: (
            "search_transcriptomic_catalogue",
            "TranscriptomicCatalogueSearchInput",
            "TranscriptomicCataloguePage",
            [
                OperationalCapability.CANDIDATE_SEARCH,
                OperationalCapability.PAGINATION,
                OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
                OperationalCapability.CONTEXT_METADATA,
                OperationalCapability.COMPOUND_SIGNATURE_OVERLAP,
            ],
        ),
        EvidenceRole.IDENTITY: (
            "inspect_compound_identifier_index",
            "CompoundIdentifierIndexInput",
            "CompoundIdentifierIndex",
            [
                OperationalCapability.COMPOUND_IDENTIFIER_INDEX,
                OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE,
            ],
        ),
        EvidenceRole.SUPPORTING_METADATA: (
            "inspect_supporting_metadata_catalogue",
            "SupportingMetadataCatalogueInput",
            "SupportingMetadataCatalogue",
            [OperationalCapability.LIGHTWEIGHT_METADATA_CATALOGUE],
        ),
    }
    tasks: list[ProviderDiscoveryTask] = []
    provider_operations: dict[str, tuple[str, str, str, str]] = {
        "toxcast": (
            "retrieve_toxcast_preapproval_metadata",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "invitrodb-v4.3-2025-08",
        ),
        "tox21": (
            "retrieve_tox21_preapproval_metadata",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "tox21-pubchem-reviewed-current",
        ),
        "pubchem-bioassay": (
            "retrieve_pubchem_bioassay_preapproval_metadata",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "pubchem-bioassay-reviewed-current",
        ),
        "pubchem-compound": (
            "resolve_pubchem_compound_full_set",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "pubchem-compound-reviewed-current",
        ),
        "ncbi-geo": (
            "retrieve_geo_preapproval_metadata",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "ncbi-geo-reviewed-current",
        ),
        "ncbi-supporting-metadata": (
            "retrieve_supporting_metadata_catalogue",
            "ProviderMetadataExecutionInput",
            "ProviderMetadataExecutionOutput",
            "ncbi-supporting-metadata-reviewed-current",
        ),
    }
    for profile in registry.providers:
        for role in profile.evidence_roles:
            operation, input_contract, output_contract, required = role_contracts[role]
            required = [
                *required,
                OperationalCapability.CACHE_REPLAY,
                OperationalCapability.VERSION_CHECKSUM_PROVENANCE,
            ]
            if profile.provider == "lincs-l1000":
                operation = "retrieve_lincs_metadata_release"
                input_contract = "LincsMetadataRetrievalInput"
                output_contract = "LincsMetadataRetrievalOutput"
                required = [
                    *required,
                    OperationalCapability.PERTURBAGEN_CATALOGUE,
                    OperationalCapability.SIGNATURE_METADATA_CATALOGUE,
                    OperationalCapability.CELL_METADATA_CATALOGUE,
                    OperationalCapability.GENE_METADATA_CATALOGUE,
                    OperationalCapability.DETERMINISTIC_COMPOUND_INDEX,
                    OperationalCapability.DETERMINISTIC_SIGNATURE_INDEX,
                    OperationalCapability.CONTEXT_COVERAGE,
                ]
            provider_operation = provider_operations.get(profile.provider)
            release_id: str | None = None
            if provider_operation is not None:
                operation, input_contract, output_contract, release_id = provider_operation
            modalities: list[str | None] = []
            if role is EvidenceRole.ACTIVITY:
                modalities.extend(requested_modalities)
            else:
                modalities.append(None)
            for modality in modalities:
                if (
                    modality is not None
                    and "*" not in profile.supported_modalities
                    and modality not in profile.supported_modalities
                ):
                    continue
                task_basis = {
                    "workflow_id": workflow_id,
                    "round": discovery_round,
                    "provider": profile.provider,
                    "role": role.value,
                    "modality": modality,
                }
                task_id = f"task-{deterministic_fingerprint(task_basis)[:24]}"
                tasks.append(
                    ProviderDiscoveryTask(
                        task_id=task_id,
                        provider=profile.provider,
                        evidence_role=role,
                        modality=modality,
                        operation=operation,
                        typed_input_contract=input_contract,
                        typed_output_contract=output_contract,
                        query_parameters={
                            "biological_target": biological_target,
                            "target_synonyms": target_synonyms,
                            "modality": modality,
                            "source_hints": [],
                            **(
                                {"release_id": "lincs-gse92742-phase1-2017"}
                                if profile.provider == "lincs-l1000"
                                else {}
                            ),
                            **({"release_id": release_id} if release_id is not None else {}),
                        },
                        required_capabilities=list(dict.fromkeys(required)),
                        pagination_policy=pagination,
                    )
                )
    if not tasks:
        raise ValueError("no operational providers apply to the approved discovery scope")
    hydration_fields = {
        EvidenceRole.ACTIVITY: [
            "source_identifier",
            "target",
            "modality",
            "activity_field",
            "compound_identifier_field",
            "assay_context",
            "source_version",
            "licence",
        ],
        EvidenceRole.TRANSCRIPTOMIC: [
            "source_identifier",
            "perturbagen_identifier",
            "signature_identifier",
            "cell_or_tissue",
            "dose",
            "exposure_time",
            "processing_level",
            "source_version",
            "licence",
        ],
        EvidenceRole.IDENTITY: [
            "source_identifier",
            "canonical_identifier",
            "mapping_status",
            "structure_field",
            "provenance",
        ],
        EvidenceRole.SUPPORTING_METADATA: [
            "source_identifier",
            "record_type",
            "relationship",
            "provenance",
        ],
    }
    allowed = sorted(
        {
            capability
            for task in tasks
            for capability in task.required_capabilities
            if capability is not OperationalCapability.HEAVY_EXPRESSION_EXTRACTION
        },
        key=str,
    )
    return DiscoveryPlan.create(
        workflow_id=workflow_id,
        endpoint_identifier=endpoint_identifier,
        approved_endpoint_specification_artifact=approved_specification_artifact,
        discovery_round=discovery_round,
        applicable_registered_providers=sorted({item.provider for item in tasks}),
        target_identifiers=[biological_target],
        target_synonyms=list(dict.fromkeys(target_synonyms)),
        requested_modalities=list(dict.fromkeys(requested_modalities)),
        provider_specific_query_tasks=tasks,
        required_hydration_fields=hydration_fields,
        allowed_preapproval_tool_capabilities=allowed,
        scientific_and_execution_budgets=budgets,
    )


def initial_execution_ledger(
    plan: DiscoveryPlan,
    registry: OperationalCapabilityRegistry,
) -> tuple[DiscoveryExecutionLedger, ProviderCapabilityFindingSet]:
    """Create a complete task ledger and explicit blockers without dropping tasks."""

    records: list[DiscoveryExecutionRecord] = []
    findings: list[RegisteredProviderCapabilityMissing] = []
    finding_keys: set[tuple[str, OperationalCapability]] = set()
    for task in plan.provider_specific_query_tasks:
        profile = registry.by_provider(task.provider)
        missing = [
            capability
            for capability in task.required_capabilities
            if not profile.declaration(capability).satisfies_preapproval_requirement
        ]
        for capability in missing:
            finding_key = (task.provider, capability)
            if finding_key in finding_keys:
                continue
            finding_keys.add(finding_key)
            declaration = profile.declaration(capability)
            findings.append(
                RegisteredProviderCapabilityMissing(
                    provider=task.provider,
                    missing_capability=capability,
                    missing_typed_tool_or_adapter_method=(
                        declaration.typed_tool_or_adapter_method or "unavailable"
                    ),
                    required_input_contract=(
                        declaration.input_contract or task.typed_input_contract
                    ),
                    required_output_contract=(
                        declaration.output_contract or task.typed_output_contract
                    ),
                    blocked_workflow_stage="DISCOVERING_SOURCE_CANDIDATES",
                    scientific_consequence=(
                        f"The registered provider cannot establish complete "
                        f"{task.evidence_role.value} coverage for its planned tasks. "
                        "No generic provider is substituted."
                    ),
                    tests_required=[
                        "typed adapter contract test",
                        "offline pagination/completion regression",
                        "artifact provenance and cache/replay regression",
                    ],
                )
            )
        records.append(
            DiscoveryExecutionRecord(
                task_id=task.task_id,
                provider=task.provider,
                evidence_role=task.evidence_role,
                modality=task.modality,
                status=(
                    DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING
                    if missing
                    else DiscoveryTaskStatus.PENDING
                ),
                completion_reason=(
                    "One or more required operational provider capabilities are missing."
                    if missing
                    else None
                ),
                error_classification=(
                    "REGISTERED_PROVIDER_CAPABILITY_MISSING" if missing else None
                ),
            )
        )
    return (
        DiscoveryExecutionLedger(
            workflow_id=plan.workflow_id,
            discovery_round=plan.discovery_round,
            plan_fingerprint=plan.plan_fingerprint,
            records=records,
        ),
        ProviderCapabilityFindingSet(
            workflow_id=plan.workflow_id,
            discovery_round=plan.discovery_round,
            findings=findings,
        ),
    )


def validate_execution_ledger(plan: DiscoveryPlan, ledger: DiscoveryExecutionLedger) -> None:
    """Ensure an LLM or caller did not remove, replace, or add planned tasks."""

    if ledger.plan_fingerprint != plan.plan_fingerprint:
        raise ValueError("ledger is not bound to the discovery plan")
    planned = {
        (task.task_id, task.provider, task.evidence_role, task.modality)
        for task in plan.provider_specific_query_tasks
    }
    recorded = {
        (record.task_id, record.provider, record.evidence_role, record.modality)
        for record in ledger.records
    }
    if recorded != planned:
        raise ValueError("execution ledger must retain every planned discovery task exactly once")


def validate_candidate_universe(
    ledger: DiscoveryExecutionLedger,
    candidates: SourceCandidateSet,
) -> None:
    if candidates.plan_fingerprint != ledger.plan_fingerprint:
        raise ValueError("source candidates are not bound to the execution ledger")
    task_ids = {item.task_id for item in ledger.records}
    unknown = sorted(
        {
            item.discovery_task_id
            for item in candidates.candidates
            if item.discovery_task_id not in task_ids
        }
    )
    if unknown:
        raise ValueError(
            f"source candidates reference unknown discovery tasks: {', '.join(unknown)}"
        )
    observed_counts: dict[str, int] = {}
    for candidate in candidates.candidates:
        contributing_tasks = candidate.contributing_task_ids or [candidate.discovery_task_id]
        for task_id in contributing_tasks:
            observed_counts[task_id] = observed_counts.get(task_id, 0) + 1
    mismatched = sorted(
        item.task_id
        for item in ledger.records
        if observed_counts.get(item.task_id, 0)
        != (
            item.compact_source_candidate_count
            if "compact_source_candidate_count" in item.model_fields_set
            else item.unique_candidate_count
        )
    )
    if mismatched:
        raise ValueError(
            "source candidates must retain the ledger's complete unique-candidate counts "
            "for the compact candidate universe: " + ", ".join(mismatched)
        )
    evidence_artifact_ids = {item.artifact_id for item in candidates.provider_dataset_artifacts} | {
        item.artifact_id
        for candidate in candidates.candidates
        for item in candidate.provider_dataset_artifacts
    }
    evidence_gaps = sorted(
        item.task_id
        for item in ledger.records
        if item.raw_record_count > 0
        and (
            item.retained_row_count != item.raw_record_count
            or not item.row_level_artifact_references
            or not item.deterministic_summary_artifacts
            or not {
                reference.artifact_id for reference in item.row_level_artifact_references
            }.issubset(evidence_artifact_ids)
        )
    )
    if evidence_gaps:
        raise ValueError(
            "provider row-level evidence is not fully retained by immutable artifacts, "
            "deterministic summaries, and task provenance: " + ", ".join(evidence_gaps)
        )
    totals = {
        "raw_record_count": sum(item.raw_record_count for item in ledger.records),
        "normalized_record_count": sum(item.normalized_record_count for item in ledger.records),
        "provider_unique_record_count": sum(
            item.provider_unique_record_count for item in ledger.records
        ),
        "compact_source_candidate_count": len(candidates.candidates),
        "retained_row_count": sum(item.retained_row_count for item in ledger.records),
    }
    for field, expected in totals.items():
        if getattr(candidates, field) not in {0, expected}:
            raise ValueError(f"source-candidate accounting summary differs for {field}")


def validate_hydrated_sources(
    candidates: SourceCandidateSet,
    hydrated: HydratedSourceSet,
) -> None:
    candidate_ids = {item.candidate_id for item in candidates.candidates}
    unknown = sorted(
        {
            item.source_candidate_id
            for item in hydrated.sources
            if item.source_candidate_id not in candidate_ids
        }
    )
    if unknown:
        raise ValueError(f"hydrated sources reference unknown candidates: {', '.join(unknown)}")
    hydration_counts: dict[str, int] = {}
    for source in hydrated.sources:
        hydration_counts[source.source_candidate_id] = (
            hydration_counts.get(source.source_candidate_id, 0) + 1
        )
    missing_or_duplicate = sorted(
        item.candidate_id
        for item in candidates.candidates
        if item.candidate_status not in {CandidateStatus.DUPLICATE, CandidateStatus.EXCLUDED}
        and hydration_counts.get(item.candidate_id, 0) != 1
    )
    if missing_or_duplicate:
        raise ValueError(
            "every retained source candidate requires exactly one hydration outcome: "
            + ", ".join(missing_or_duplicate)
        )


def validate_coverage_universe(
    hydrated: HydratedSourceSet,
    coverage: CombinationCoverageSet,
) -> None:
    source_ids = {item.hydrated_source_id for item in hydrated.sources}
    unknown = sorted(
        {
            source_id
            for item in coverage.combinations
            for source_id in [*item.activity_source_ids, item.transcriptomic_source_id]
            if source_id not in source_ids
        }
    )
    if unknown:
        raise ValueError(f"coverage references unknown hydrated sources: {', '.join(unknown)}")
    usable_activity = [
        item
        for item in hydrated.sources
        if item.evidence_role is EvidenceRole.ACTIVITY
        and item.completeness_status is not HydrationCompleteness.SCIENTIFICALLY_UNUSABLE
        and item.exclusion_reason is None
    ]
    usable_transcriptomic = [
        item
        for item in hydrated.sources
        if item.evidence_role is EvidenceRole.TRANSCRIPTOMIC
        and item.completeness_status is not HydrationCompleteness.SCIENTIFICALLY_UNUSABLE
        and item.exclusion_reason is None
    ]
    observed_pairs = {
        (item.activity_source_ids[0], item.transcriptomic_source_id, item.requested_modality)
        for item in coverage.combinations
        if len(item.activity_source_ids) == 1
    }
    missing_pairs = sorted(
        (
            activity.hydrated_source_id,
            transcriptomic.hydrated_source_id,
            str(activity.modality),
        )
        for activity in usable_activity
        for transcriptomic in usable_transcriptomic
        if (
            activity.hydrated_source_id,
            transcriptomic.hydrated_source_id,
            str(activity.modality),
        )
        not in observed_pairs
    )
    if missing_pairs:
        raise ValueError(
            "combination coverage must retain every usable activity/transcriptomic pair: "
            + ", ".join("/".join(item) for item in missing_pairs)
        )


def validate_strategy_universe(
    coverage: CombinationCoverageSet,
    proposals: StrategyProposalSet,
    *,
    allowed_scientific_policies: set[str] | None = None,
) -> None:
    allowed_policies = allowed_scientific_policies or {
        "source_backed_activity_call",
        "functional_or",
    }
    source_combinations = {
        frozenset([*item.activity_source_ids, item.transcriptomic_source_id])
        for item in coverage.combinations
    }
    invalid = [
        item.proposal_id
        for item in proposals.proposals
        if frozenset(item.included_source_combination) not in source_combinations
    ]
    if invalid:
        raise ValueError(
            "strategy proposals must be derived from computed source combinations: "
            + ", ".join(sorted(invalid))
        )
    unsupported = sorted(
        item.proposal_id
        for item in proposals.proposals
        if item.proposed_label_policy.get("operator") not in allowed_policies
    )
    if unsupported:
        raise ValueError(
            "strategy proposals reference unsupported scientific policies: "
            + ", ".join(unsupported)
        )
