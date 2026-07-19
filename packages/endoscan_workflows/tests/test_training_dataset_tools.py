from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from endoscan_workflows.training_dataset import (
    CapabilityStatus,
    ComponentRole,
    JoinabilityStatus,
)
from endoscan_workflows.training_dataset_tools import (
    OFFICIAL_SOURCE_ADAPTERS,
    ActivitySourceSearchInput,
    CoverageAuditInput,
    IdentityFieldInspectionInput,
    MappingManifestInput,
    OfficialSourceAdapterDefinition,
    OfficialSourceSystem,
    RecordAvailabilityInput,
    SourceAdapterRegistry,
    SourceCandidate,
    SourceValidationOutput,
    StableSourceIdentifierInput,
    TranscriptomicSourceSearchInput,
    adapter_capability_inventory,
    audit_coverage,
    build_compound_mapping_manifest,
    estimate_or_compute_overlap,
    inspect_source_identity_fields,
    inspect_source_record_availability,
)


@dataclass
class SyntheticAdapter:
    definition: OfficialSourceAdapterDefinition
    candidates: list[SourceCandidate]

    def search_activity(self, _request: ActivitySourceSearchInput) -> list[SourceCandidate]:
        return [item for item in self.candidates if ComponentRole.ENDPOINT_ACTIVITY in item.roles]

    def search_transcriptomics(
        self, _request: TranscriptomicSourceSearchInput
    ) -> list[SourceCandidate]:
        return [
            item for item in self.candidates if ComponentRole.TRANSCRIPTOMIC_MATRIX in item.roles
        ]

    def validate(self, request: StableSourceIdentifierInput) -> SourceValidationOutput:
        candidate = next(
            (
                item
                for item in self.candidates
                if item.stable_identifier == request.stable_identifier
            ),
            None,
        )
        return SourceValidationOutput(
            source=candidate,
            public_available=candidate is not None,
            stable_identifier_valid=candidate is not None,
            capability_status=(
                CapabilityStatus.VERIFIED_AVAILABLE if candidate else CapabilityStatus.UNAVAILABLE
            ),
        )


def candidate(
    source_system: OfficialSourceSystem,
    identifier: str,
    roles: list[ComponentRole],
    *,
    modality: str,
) -> SourceCandidate:
    return SourceCandidate(
        source_id=f"{source_system.value}-{identifier}",
        source_system=source_system,
        stable_identifier=identifier,
        title=f"Synthetic {identifier}",
        roles=roles,
        modality=modality,
        public_metadata_available=True,
        result_table_status=CapabilityStatus.METADATA_ONLY,
        identifier_fields=["PubChem CID"],
        capability_fields=["activity"],
        source_references=[f"artifact:{identifier}"],
        evidence_references=[f"artifact:{identifier}#metadata"],
    )


def reviewed_definition(system: OfficialSourceSystem) -> OfficialSourceAdapterDefinition:
    return next(item for item in OFFICIAL_SOURCE_ADAPTERS if item.source_system is system)


def test_official_registry_has_typed_roles_hosts_and_no_large_discovery_downloads() -> None:
    inventory = adapter_capability_inventory()
    assert inventory
    assert all(item["allowed_hosts"] for item in inventory)
    assert all(item["large_downloads_during_discovery"] is False for item in inventory)
    assert not any("thyroid" in str(item).casefold() for item in inventory)


def test_stable_identifier_rejects_urls_paths_and_unsupported_characters() -> None:
    for invalid in ("https://example.invalid/a", "../secret", "AID 123"):
        with pytest.raises(ValidationError):
            StableSourceIdentifierInput(
                source_system=OfficialSourceSystem.PUBCHEM_BIOASSAY,
                stable_identifier=invalid,
            )


def test_activity_discovery_supports_one_or_multiple_related_assays() -> None:
    system = OfficialSourceSystem.PUBCHEM_BIOASSAY
    registry = SourceAdapterRegistry(
        [
            SyntheticAdapter(
                reviewed_definition(system),
                [
                    candidate(
                        system, "AID-1", [ComponentRole.ENDPOINT_ACTIVITY], modality="binding"
                    ),
                    candidate(
                        system, "AID-2", [ComponentRole.ENDPOINT_ACTIVITY], modality="functional"
                    ),
                ],
            )
        ]
    )
    result = registry.search_activity(
        ActivitySourceSearchInput(
            biological_target="example receptor",
            endpoint_modality="antagonism",
        )
    )
    assert result.result_count == 2
    assert result.large_tables_downloaded is False
    assert result.exact_counts_computed is False


def test_wrong_activity_modality_remains_visible_for_later_rejection() -> None:
    system = OfficialSourceSystem.PUBCHEM_BIOASSAY
    wrong = candidate(system, "AID-BIND", [ComponentRole.ENDPOINT_ACTIVITY], modality="binding")
    registry = SourceAdapterRegistry([SyntheticAdapter(reviewed_definition(system), [wrong])])
    result = registry.search_activity(
        ActivitySourceSearchInput(
            biological_target="example receptor",
            endpoint_modality="functional antagonism",
        )
    )
    assert result.candidates[0].modality == "binding"
    assert result.candidates[0].validation_status == "metadata_candidate"


def test_transcriptomic_search_requires_chemical_perturbation() -> None:
    with pytest.raises(ValidationError):
        TranscriptomicSourceSearchInput(perturbation_type="disease cohort")
    with pytest.raises(ValidationError):
        TranscriptomicSourceSearchInput(perturbation_type="genetic perturbation")


def test_transcriptomic_search_supports_several_resources() -> None:
    systems = [OfficialSourceSystem.GEO, OfficialSourceSystem.PERTURBATIONAL_TRANSCRIPTOMICS]
    registry = SourceAdapterRegistry(
        [
            SyntheticAdapter(
                reviewed_definition(system),
                [
                    candidate(
                        system,
                        f"TX-{index}",
                        [ComponentRole.TRANSCRIPTOMIC_MATRIX],
                        modality="chemical perturbation",
                    )
                ],
            )
            for index, system in enumerate(systems)
        ]
    )
    result = registry.search_transcriptomics(
        TranscriptomicSourceSearchInput(perturbation_type="chemical perturbation")
    )
    assert result.result_count == 2


def test_registry_rejects_unreviewed_hosts() -> None:
    definition = OfficialSourceAdapterDefinition(
        source_system=OfficialSourceSystem.PUBCHEM_BIOASSAY,
        roles=(ComponentRole.ENDPOINT_ACTIVITY,),
        allowed_hosts=("unreviewed.invalid",),
        operations=("search_activity_sources",),
        identifier_types=("AID",),
    )
    with pytest.raises(ValueError, match="reviewed"):
        SourceAdapterRegistry().register(SyntheticAdapter(definition, []))


def test_unconfigured_adapter_fails_closed_without_fabricated_source() -> None:
    result = SourceAdapterRegistry().validate(
        StableSourceIdentifierInput(
            source_system=OfficialSourceSystem.PUBCHEM_BIOASSAY,
            stable_identifier="AID-1",
        )
    )
    assert result.source is None
    assert result.capability_status is CapabilityStatus.UNRESOLVED


def test_identity_fields_find_common_pubchem_bridge() -> None:
    result = inspect_source_identity_fields(
        IdentityFieldInspectionInput(
            source_ids=["activity", "transcriptomics"],
            inventory_identifier_fields={
                "activity": ["PubChem CID", "assay compound ID"],
                "transcriptomics": ["PubChem CID", "perturbagen ID"],
            },
        )
    )
    assert result.common_identifier_fields == ["PubChem CID"]
    assert result.status is JoinabilityStatus.METADATA_ONLY


def test_no_common_identifier_requires_manual_mapping() -> None:
    result = inspect_source_identity_fields(
        IdentityFieldInspectionInput(
            source_ids=["activity", "transcriptomics"],
            inventory_identifier_fields={
                "activity": ["assay compound ID"],
                "transcriptomics": ["perturbagen ID"],
            },
        )
    )
    assert result.common_identifier_fields == []
    assert result.status is JoinabilityStatus.REQUIRES_MANUAL_MAPPING


def test_record_availability_distinguishes_download_and_unavailable() -> None:
    result = inspect_source_record_availability(
        RecordAvailabilityInput(
            source_ids=["a", "b", "c"],
            access_status_by_source={
                "a": CapabilityStatus.VERIFIED_AVAILABLE,
                "b": CapabilityStatus.REQUIRES_DOWNLOAD,
                "c": CapabilityStatus.UNAVAILABLE,
            },
        )
    )
    assert result.requires_download == ["b"]
    assert result.unavailable == ["c"]


def test_mapping_manifest_deduplicates_and_computes_exact_overlap() -> None:
    result = build_compound_mapping_manifest(
        MappingManifestInput(
            source_identifier_records={
                "activity": ["CID1", "CID1", "CID2"],
                "transcriptomics": ["CID2", "CID3"],
            },
            canonical_identifier_type="PubChem CID",
        )
    )
    assert result.duplicate_identifiers == {"activity": ["CID1"], "transcriptomics": []}
    assert result.exact_overlap_identifiers == ["CID2"]
    assert result.status is JoinabilityStatus.COMPUTED_EXACT


def test_overlap_is_not_fabricated_without_loaded_identifier_tables() -> None:
    diagnostic = estimate_or_compute_overlap(None, source_ids=["activity", "transcriptomics"])
    assert diagnostic.status is JoinabilityStatus.REQUIRES_DOWNLOAD
    assert diagnostic.exact_overlap_count is None


def test_exact_overlap_is_computed_when_tables_are_present() -> None:
    diagnostic = estimate_or_compute_overlap(
        {"activity": ["CID1", "CID2"], "transcriptomics": ["CID2", "CID3"]},
        source_ids=["activity", "transcriptomics"],
    )
    assert diagnostic.status is JoinabilityStatus.COMPUTED_EXACT
    assert diagnostic.exact_overlap_count == 1


def test_coverage_requires_download_until_full_tables_are_loaded() -> None:
    pending = audit_coverage(
        CoverageAuditInput(
            expected_identifiers=["CID1", "CID2"],
            observed_identifiers=["CID1"],
            full_tables_loaded=False,
        )
    )
    exact = audit_coverage(
        CoverageAuditInput(
            expected_identifiers=["CID1", "CID2"],
            observed_identifiers=["CID1"],
            full_tables_loaded=True,
        )
    )
    assert pending.status is JoinabilityStatus.REQUIRES_DOWNLOAD
    assert pending.coverage is None
    assert exact.status is JoinabilityStatus.COMPUTED_EXACT
    assert exact.coverage == 0.5
