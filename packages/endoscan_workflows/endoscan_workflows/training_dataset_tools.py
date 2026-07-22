"""Typed source-adapter and deterministic assembly-tool architecture.

Adapters expose reviewed operations rather than arbitrary URLs.  The registry in
this module is metadata-only; network implementations are injected separately and
offline tests use synthetic records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .training_dataset import (
    CapabilityStatus,
    ComponentRole,
    JoinabilityDiagnostic,
    JoinabilityStatus,
)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0.0"


class OfficialSourceSystem(StrEnum):
    PUBCHEM_BIOASSAY = "pubchem_bioassay"
    TOX21 = "tox21"
    TOXCAST = "toxcast"
    GEO = "geo"
    PERTURBATIONAL_TRANSCRIPTOMICS = "perturbational_transcriptomics"
    PUBCHEM_COMPOUND = "pubchem_compound"


@dataclass(frozen=True)
class OfficialSourceAdapterDefinition:
    source_system: OfficialSourceSystem
    roles: tuple[ComponentRole, ...]
    allowed_hosts: tuple[str, ...]
    operations: tuple[str, ...]
    identifier_types: tuple[str, ...]
    allows_large_downloads_during_discovery: bool = False


OFFICIAL_SOURCE_ADAPTERS: tuple[OfficialSourceAdapterDefinition, ...] = (
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.PUBCHEM_BIOASSAY,
        roles=(ComponentRole.ENDPOINT_ACTIVITY, ComponentRole.ASSAY_METADATA),
        allowed_hosts=("pubchem.ncbi.nlm.nih.gov", "eutils.ncbi.nlm.nih.gov"),
        operations=(
            "search_activity_sources",
            "validate_activity_source",
            "fetch_activity_source_metadata",
            "inspect_activity_result_availability",
            "inspect_activity_identifier_fields",
            "summarize_activity_outcomes",
            "inspect_counter_screen_relationships",
        ),
        identifier_types=("PubChem AID", "PubChem CID", "InChIKey"),
    ),
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.TOX21,
        roles=(ComponentRole.ENDPOINT_ACTIVITY, ComponentRole.ASSAY_METADATA),
        allowed_hosts=("comptox.epa.gov",),
        operations=(
            "search_activity_sources",
            "validate_activity_source",
            "fetch_activity_source_metadata",
            "inspect_activity_result_availability",
        ),
        identifier_types=("DTXSID", "PubChem CID", "CASRN"),
    ),
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.TOXCAST,
        roles=(
            ComponentRole.ENDPOINT_ACTIVITY,
            ComponentRole.ASSAY_METADATA,
            ComponentRole.COUNTER_SCREEN,
        ),
        allowed_hosts=("comptox.epa.gov",),
        operations=(
            "search_activity_sources",
            "validate_activity_source",
            "fetch_activity_source_metadata",
            "inspect_counter_screen_relationships",
        ),
        identifier_types=("DTXSID", "PubChem CID", "CASRN"),
    ),
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.GEO,
        roles=(
            ComponentRole.TRANSCRIPTOMIC_MATRIX,
            ComponentRole.TRANSCRIPTOMIC_CONDITIONS,
            ComponentRole.SAMPLE_METADATA,
        ),
        allowed_hosts=("eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"),
        operations=(
            "search_transcriptomic_sources",
            "validate_transcriptomic_source",
            "fetch_transcriptomic_source_metadata",
            "inspect_perturbation_design",
            "inspect_signature_conditions",
            "inspect_processed_matrix_availability",
            "inspect_raw_matrix_availability",
            "inspect_feature_schema",
        ),
        identifier_types=("GSE", "GSM", "PubChem CID", "CASRN", "compound name"),
    ),
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.PERTURBATIONAL_TRANSCRIPTOMICS,
        roles=(
            ComponentRole.TRANSCRIPTOMIC_MATRIX,
            ComponentRole.TRANSCRIPTOMIC_CONDITIONS,
            ComponentRole.COMPOUND_IDENTITY,
        ),
        allowed_hosts=("clue.io", "lincsproject.org"),
        operations=(
            "search_transcriptomic_sources",
            "validate_transcriptomic_source",
            "fetch_transcriptomic_source_metadata",
            "inspect_perturbation_design",
            "inspect_transcriptomic_identity_fields",
            "inspect_signature_conditions",
            "inspect_processed_matrix_availability",
            "inspect_feature_schema",
        ),
        identifier_types=("perturbagen ID", "InChIKey", "canonical SMILES"),
    ),
    OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.PUBCHEM_COMPOUND,
        roles=(ComponentRole.COMPOUND_IDENTITY, ComponentRole.CHEMICAL_STRUCTURE),
        allowed_hosts=("pubchem.ncbi.nlm.nih.gov",),
        operations=(
            "inspect_source_identity_fields",
            "inspect_source_record_availability",
            "build_compound_mapping_manifest",
            "resolve_compound_identity_sample",
        ),
        identifier_types=("PubChem CID", "InChIKey", "canonical SMILES", "isomeric SMILES"),
    ),
)


def adapter_capability_inventory() -> list[dict[str, object]]:
    return [
        {
            "source_system": item.source_system.value,
            "roles": [role.value for role in item.roles],
            "allowed_hosts": list(item.allowed_hosts),
            "operations": list(item.operations),
            "identifier_types": list(item.identifier_types),
            "large_downloads_during_discovery": item.allows_large_downloads_during_discovery,
        }
        for item in OFFICIAL_SOURCE_ADAPTERS
    ]


class ActivitySourceSearchInput(ToolInput):
    biological_target: str = Field(min_length=1, max_length=500)
    endpoint_modality: str = Field(min_length=1, max_length=300)
    measurement_types: list[str] = Field(default_factory=list, max_length=20)
    source_systems: list[OfficialSourceSystem] = Field(default_factory=list, max_length=10)
    maximum_results_per_source: int = Field(default=10, ge=1, le=25)


class TranscriptomicSourceSearchInput(ToolInput):
    perturbation_type: str = Field(pattern=r"^chemical(?: perturbation)?$", max_length=40)
    organisms: list[str] = Field(default_factory=list, max_length=10)
    biological_contexts: list[str] = Field(default_factory=list, max_length=20)
    required_metadata: list[str] = Field(default_factory=list, max_length=30)
    source_systems: list[OfficialSourceSystem] = Field(default_factory=list, max_length=10)
    maximum_results_per_source: int = Field(default=10, ge=1, le=25)


class StableSourceIdentifierInput(ToolInput):
    source_system: OfficialSourceSystem
    stable_identifier: str = Field(min_length=1, max_length=200)

    @field_validator("stable_identifier")
    @classmethod
    def reject_urls(cls, value: str) -> str:
        if "://" in value or value.startswith(("/", "\\")):
            raise ValueError("stable_identifier must not be a URL or path")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            raise ValueError("stable_identifier contains unsupported characters")
        return value


class SourceCandidate(ToolOutput):
    source_id: str
    source_system: OfficialSourceSystem
    stable_identifier: str
    title: str
    roles: list[ComponentRole]
    modality: str | None = None
    public_metadata_available: bool
    result_table_status: CapabilityStatus
    identifier_fields: list[str] = Field(default_factory=list)
    capability_fields: list[str] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    validation_status: str = "metadata_candidate"


class SourceSearchOutput(ToolOutput):
    candidates: list[SourceCandidate] = Field(default_factory=list, max_length=100)
    result_count: int = Field(ge=0)
    source_systems_queried: list[OfficialSourceSystem] = Field(default_factory=list)
    exact_counts_computed: bool = False
    large_tables_downloaded: bool = False


class SourceValidationOutput(ToolOutput):
    source: SourceCandidate | None = None
    public_available: bool
    stable_identifier_valid: bool
    capability_status: CapabilityStatus
    evidence_references: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class IdentityFieldInspectionInput(ToolInput):
    source_ids: list[str] = Field(min_length=1, max_length=50)
    inventory_identifier_fields: dict[str, list[str]] = Field(default_factory=dict)


class IdentityFieldInspectionOutput(ToolOutput):
    fields_by_source: dict[str, list[str]]
    common_identifier_fields: list[str]
    status: JoinabilityStatus


class RecordAvailabilityInput(ToolInput):
    source_ids: list[str] = Field(min_length=1, max_length=50)
    access_status_by_source: dict[str, CapabilityStatus]


class RecordAvailabilityOutput(ToolOutput):
    source_statuses: dict[str, CapabilityStatus]
    requires_download: list[str]
    unavailable: list[str]


class MappingManifestInput(ToolInput):
    source_identifier_records: dict[str, list[str]]
    canonical_identifier_type: str = Field(min_length=1, max_length=100)


class MappingManifestOutput(ToolOutput):
    source_ids: list[str]
    canonical_identifier_type: str
    normalized_records: dict[str, list[str]]
    duplicate_identifiers: dict[str, list[str]]
    exact_overlap_identifiers: list[str]
    status: JoinabilityStatus


class CoverageAuditInput(ToolInput):
    expected_identifiers: list[str] = Field(default_factory=list, max_length=100_000)
    observed_identifiers: list[str] = Field(default_factory=list, max_length=100_000)
    full_tables_loaded: bool = False


class CoverageAuditOutput(ToolOutput):
    status: JoinabilityStatus
    observed_count: int = Field(ge=0)
    matched_count: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)
    required_computation: str | None = None


class SourceAdapter(Protocol):
    definition: OfficialSourceAdapterDefinition

    def search_activity(self, request: ActivitySourceSearchInput) -> list[SourceCandidate]: ...

    def search_transcriptomics(
        self, request: TranscriptomicSourceSearchInput
    ) -> list[SourceCandidate]: ...

    def validate(self, request: StableSourceIdentifierInput) -> SourceValidationOutput: ...


class SourceAdapterRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter] = ()):
        self._adapters = {item.definition.source_system: item for item in adapters}

    def register(self, adapter: SourceAdapter) -> None:
        if adapter.definition.source_system in self._adapters:
            raise ValueError(f"adapter already registered: {adapter.definition.source_system}")
        reviewed = {item.source_system: item for item in OFFICIAL_SOURCE_ADAPTERS}
        expected = reviewed.get(adapter.definition.source_system)
        if expected is None or adapter.definition.allowed_hosts != expected.allowed_hosts:
            raise ValueError("adapter definition is not in the reviewed official registry")
        self._adapters[adapter.definition.source_system] = adapter

    def _selected(
        self, systems: list[OfficialSourceSystem], role: ComponentRole
    ) -> list[SourceAdapter]:
        selected = systems or sorted(self._adapters, key=str)
        return [
            self._adapters[system]
            for system in selected
            if system in self._adapters and role in self._adapters[system].definition.roles
        ]

    def search_activity(self, request: ActivitySourceSearchInput) -> SourceSearchOutput:
        adapters = self._selected(request.source_systems, ComponentRole.ENDPOINT_ACTIVITY)
        candidates = [
            candidate for adapter in adapters for candidate in adapter.search_activity(request)
        ]
        candidates = candidates[: request.maximum_results_per_source * max(1, len(adapters))]
        return SourceSearchOutput(
            candidates=candidates,
            result_count=len(candidates),
            source_systems_queried=[item.definition.source_system for item in adapters],
        )

    def search_transcriptomics(
        self, request: TranscriptomicSourceSearchInput
    ) -> SourceSearchOutput:
        adapters = self._selected(request.source_systems, ComponentRole.TRANSCRIPTOMIC_MATRIX)
        candidates = [
            candidate
            for adapter in adapters
            for candidate in adapter.search_transcriptomics(request)
        ]
        candidates = candidates[: request.maximum_results_per_source * max(1, len(adapters))]
        return SourceSearchOutput(
            candidates=candidates,
            result_count=len(candidates),
            source_systems_queried=[item.definition.source_system for item in adapters],
        )

    def validate(self, request: StableSourceIdentifierInput) -> SourceValidationOutput:
        adapter = self._adapters.get(request.source_system)
        if adapter is None:
            return SourceValidationOutput(
                public_available=False,
                stable_identifier_valid=False,
                capability_status=CapabilityStatus.UNRESOLVED,
                limitations=["Reviewed adapter implementation is not configured."],
            )
        return adapter.validate(request)


def inspect_source_identity_fields(
    request: IdentityFieldInspectionInput,
) -> IdentityFieldInspectionOutput:
    fields = {
        source_id: sorted(set(request.inventory_identifier_fields.get(source_id, [])))
        for source_id in request.source_ids
    }
    non_empty = [set(value) for value in fields.values() if value]
    common = (
        sorted(set.intersection(*non_empty)) if len(non_empty) == len(fields) and fields else []
    )
    return IdentityFieldInspectionOutput(
        fields_by_source=fields,
        common_identifier_fields=common,
        status=(
            JoinabilityStatus.METADATA_ONLY if common else JoinabilityStatus.REQUIRES_MANUAL_MAPPING
        ),
    )


def inspect_source_record_availability(
    request: RecordAvailabilityInput,
) -> RecordAvailabilityOutput:
    statuses = {
        source_id: request.access_status_by_source.get(source_id, CapabilityStatus.UNRESOLVED)
        for source_id in request.source_ids
    }
    return RecordAvailabilityOutput(
        source_statuses=statuses,
        requires_download=[
            source_id
            for source_id, status in statuses.items()
            if status is CapabilityStatus.REQUIRES_DOWNLOAD
        ],
        unavailable=[
            source_id
            for source_id, status in statuses.items()
            if status is CapabilityStatus.UNAVAILABLE
        ],
    )


def build_compound_mapping_manifest(request: MappingManifestInput) -> MappingManifestOutput:
    normalized = {
        source_id: sorted({value.strip() for value in values if value.strip()})
        for source_id, values in request.source_identifier_records.items()
    }
    duplicates = {
        source_id: sorted(
            {
                value.strip()
                for value in values
                if value.strip() and sum(item.strip() == value.strip() for item in values) > 1
            }
        )
        for source_id, values in request.source_identifier_records.items()
    }
    sets = [set(values) for values in normalized.values()]
    overlap = sorted(set.intersection(*sets)) if sets else []
    return MappingManifestOutput(
        source_ids=sorted(normalized),
        canonical_identifier_type=request.canonical_identifier_type,
        normalized_records=normalized,
        duplicate_identifiers=duplicates,
        exact_overlap_identifiers=overlap,
        status=JoinabilityStatus.COMPUTED_EXACT,
    )


def audit_coverage(request: CoverageAuditInput) -> CoverageAuditOutput:
    observed = set(request.observed_identifiers)
    if not request.full_tables_loaded:
        return CoverageAuditOutput(
            status=JoinabilityStatus.REQUIRES_DOWNLOAD,
            observed_count=len(observed),
            required_computation=(
                "Load the approved source tables and compute exact identifier overlap."
            ),
        )
    expected = set(request.expected_identifiers)
    matched = len(expected & observed)
    return CoverageAuditOutput(
        status=JoinabilityStatus.COMPUTED_EXACT,
        observed_count=len(observed),
        matched_count=matched,
        coverage=(matched / len(expected) if expected else 1.0),
    )


def estimate_or_compute_overlap(
    source_identifier_records: Mapping[str, list[str]] | None,
    *,
    source_ids: list[str],
) -> JoinabilityDiagnostic:
    if source_identifier_records is None:
        return JoinabilityDiagnostic(
            diagnostic_id="joinability-requires-download",
            status=JoinabilityStatus.REQUIRES_DOWNLOAD,
            source_ids=source_ids,
            required_downloads=["source compound identifier tables"],
            required_computations=["canonicalize identifiers", "compute exact intersection"],
        )
    manifest = build_compound_mapping_manifest(
        MappingManifestInput(
            source_identifier_records=dict(source_identifier_records),
            canonical_identifier_type="provided canonical identifier",
        )
    )
    return JoinabilityDiagnostic(
        diagnostic_id="joinability-computed-exact",
        status=JoinabilityStatus.COMPUTED_EXACT,
        source_ids=manifest.source_ids,
        identifier_type=manifest.canonical_identifier_type,
        exact_overlap_count=len(manifest.exact_overlap_identifiers),
    )
