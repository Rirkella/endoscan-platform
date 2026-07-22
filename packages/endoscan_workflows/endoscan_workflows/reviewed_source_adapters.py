"""Versioned, reviewed adapters for bounded official-source discovery.

The model selects an approved operation and typed arguments.  It never supplies a URL,
host, HTTP method, parser, or transport policy.  Network access is injected so the same
contracts are exercised by offline fixtures and the production client.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from collections.abc import Iterable
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any, Literal, cast
from urllib.parse import quote, urljoin, urlparse

from pydantic import Field, field_validator, model_validator

from .artifacts import LocalArtifactStore
from .contracts import SourceToolDiagnostic, StrictContract, ToolInvocation
from .repository import canonical_json, deterministic_id, utc_text
from .source_cache import SourceResponseCache
from .source_security import (
    ALLOWED_CONTENT_TYPES,
    TECHNICAL_HEALTH_STATUS_MIME,
    ScientificSourceClient,
    SourceToolError,
    sanitize_untrusted_text,
)
from .training_dataset import (
    ActivityEvidenceRow,
    CapabilityStatus,
    ComponentRole,
    CompoundIdentityBridgeRow,
    ObservationCountStatus,
    SourceValidationStatus,
    TranscriptomicProfileEvidenceRow,
    VerifiedSourceObservation,
    VerifiedSourceObservationBatch,
    VerifiedSourceSearchOutcome,
)

REVIEW_POLICY_VERSION = "reviewed-source-adapters-v1"
EPA_TOXCAST_PUBLIC_RELEASE_PAGE = "https://clowder.edap-cluster.com/spaces/687e388ce4b02565bc3e28e4"
EPA_PUBLIC_DISTRIBUTION_HOSTS = frozenset(
    {
        "api.figshare.com",
        "clowder.edap-cluster.com",
        "doi.org",
        "epa.figshare.com",
        "gaftp.epa.gov",
        "ndownloader.figshare.com",
    }
)
EPA_AUTHENTICATED_OPERATIONS = frozenset(
    {
        "search_epa_assays",
        "inspect_epa_assay_metadata",
        "inspect_epa_activity_availability",
        "inspect_epa_compound_identifier_fields",
        "inspect_epa_release_manifest",
        "inspect_epa_related_assay_components",
    }
)


class _BoundedManifestHTMLParser(HTMLParser):
    """Collect bounded page text and links without executing or fetching embedded content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.metadata: list[str] = []
        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "meta" and len(self.metadata) < 50:
            attributes = {name.casefold(): value for name, value in attrs if value}
            name = (attributes.get("name") or attributes.get("property") or "").casefold()
            if name in {"description", "og:description", "og:title", "twitter:title"}:
                self.metadata.append((attributes.get("content") or "")[:2_000])
            return
        if normalized_tag != "a" or len(self.links) >= 100:
            return
        href = next((value for name, value in attrs if name.casefold() == "href"), None)
        if href:
            self._active_href = href[:2_000]
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if sum(len(item) for item in self.text_parts) < 100_000:
            self.text_parts.append(data[:10_000])
        if self._active_href is not None and sum(len(item) for item in self._active_text) < 500:
            self._active_text.append(data[:500])

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._active_href is None:
            return
        label = " ".join(" ".join(self._active_text).split())[:500]
        self.links.append((self._active_href, label))
        self._active_href = None
        self._active_text = []


class _ReviewedManifestParseError(ValueError):
    """Controlled parser failure with a bounded, allowlisted diagnostic."""

    def __init__(self, category: str, stage: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.category = category
        self.stage = stage
        self.safe_message = safe_message


class AdapterReviewStatus(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class ReviewedSourceAdapterDefinition(StrictContract):
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    official_source_system: str = Field(min_length=2, max_length=160)
    supported_component_roles: list[ComponentRole] = Field(min_length=1, max_length=30)
    approved_operations: list[str] = Field(min_length=1, max_length=50)
    allowlisted_domains: list[str] = Field(min_length=1, max_length=10)
    http_methods: list[Literal["GET"]] = Field(default_factory=lambda: ["GET"])
    request_builder: str = Field(min_length=3, max_length=160)
    typed_response_parser: str = Field(min_length=3, max_length=160)
    accepted_mime_types: list[str] = Field(min_length=1, max_length=10)
    maximum_response_bytes: int = Field(ge=1_024, le=5_000_000)
    request_timeout_seconds: float = Field(gt=0, le=180)
    maximum_redirects: int = Field(ge=0, le=3)
    source_retry_count: int = Field(ge=0, le=2)
    requests_per_second: float = Field(gt=0, le=10)
    cache_ttl_seconds: int = Field(ge=60, le=2_592_000)
    allows_bulk_downloads_during_discovery: Literal[False] = False
    provenance_format: str = Field(min_length=3, max_length=200)
    health_probe_capable: bool
    review_status: AdapterReviewStatus
    review_policy_version: str = REVIEW_POLICY_VERSION

    @field_validator("allowlisted_domains")
    @classmethod
    def domains_are_hosts(cls, values: list[str]) -> list[str]:
        for value in values:
            if "://" in value or "/" in value or "@" in value:
                raise ValueError("adapter domains must be exact host names")
        return values


class ReviewedSourceOperationInput(StrictContract):
    source_system: str | None = Field(default=None, max_length=160)
    query: str | None = Field(default=None, max_length=500)
    biological_target: str | None = Field(default=None, max_length=500)
    endpoint_modality: str | None = Field(default=None, max_length=300)
    stable_identifier: str | None = Field(default=None, max_length=300)
    identifier_type: str | None = Field(default=None, max_length=80)
    sampled_identifiers: list[str] = Field(default_factory=list, max_length=10)
    source_identifiers: list[str] = Field(default_factory=list, max_length=10)
    verified_compound_names: list[str] = Field(default_factory=list, max_length=5)
    required_fields: list[str] = Field(default_factory=list, max_length=30)
    maximum_results: int = Field(default=8, ge=1, le=10)

    @field_validator("stable_identifier")
    @classmethod
    def stable_identifier_is_not_a_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "://" in value or value.startswith(("/", "\\")):
            raise ValueError("stable identifiers must not be URLs or paths")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,300}", value):
            raise ValueError("stable identifier contains unsupported characters")
        return value

    @field_validator("sampled_identifiers", "source_identifiers")
    @classmethod
    def identifier_lists_are_bounded(cls, values: list[str]) -> list[str]:
        for value in values:
            if "://" in value or len(value) > 300:
                raise ValueError("identifier lists must contain bounded non-URL values")
        return values

    @field_validator("verified_compound_names")
    @classmethod
    def verified_names_are_bounded_source_values(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            compact = re.sub(r"\s+", " ", value).strip()
            if not compact or len(compact) > 500 or "://" in compact:
                raise ValueError("verified compound names must be bounded non-URL values")
            cleaned.append(compact)
        return list(dict.fromkeys(cleaned))


class ActivityDiscoveryModality(StrEnum):
    BINDING = "binding"
    AGONISM = "agonism"
    ANTAGONISM = "antagonism"


class ActivitySearchOperationInput(ReviewedSourceOperationInput):
    """One controlled activity modality per bounded official-source search."""

    source_system: Literal["pubchem-bioassay", "PubChem BioAssay"] | None = None
    endpoint_modality: ActivityDiscoveryModality


class ActivityResultExtractionInput(ReviewedSourceOperationInput):
    """Deterministic bounded extraction for selected PubChem assay result tables."""

    source_system: Literal["pubchem-bioassay", "PubChem BioAssay"] | None = None
    source_identifiers: list[str] = Field(min_length=1, max_length=5)
    modality_by_source: dict[str, ActivityDiscoveryModality] = Field(min_length=1, max_length=5)
    maximum_rows_per_assay: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def every_assay_has_a_modality(self) -> ActivityResultExtractionInput:
        if set(self.source_identifiers) - set(self.modality_by_source):
            raise ValueError("every selected assay requires an explicit preserved modality")
        return self


class CompoundSourceOperationInput(ReviewedSourceOperationInput):
    source_system: Literal["pubchem-compound", "PubChem Compound"] | None = None

    @model_validator(mode="after")
    def require_bounded_compound_identifiers(self) -> CompoundSourceOperationInput:
        if not self.sampled_identifiers and not self.stable_identifier:
            raise ValueError("compound inspection requires stable sampled identifiers")
        return self


class SupportingSourceOperationInput(ReviewedSourceOperationInput):
    source_system: Literal["ncbi-supporting-metadata", "NCBI linked metadata"] | None = None

    @model_validator(mode="after")
    def require_linked_source_identifier(self) -> SupportingSourceOperationInput:
        if not self.source_identifiers and not self.stable_identifier:
            raise ValueError("supporting metadata inspection requires stable source identifiers")
        return self


class ReviewedSourceInvocationError(ValueError):
    """Safe contract failure raised before a scientific-source request starts."""

    def __init__(
        self,
        safe_message: str,
        *,
        invocation_stage: str,
        validation_error_category: str,
        adapter_resolution_status: str = "not_started",
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(safe_message[:1000])
        self.safe_message = safe_message[:1000]
        self.invocation_stage = invocation_stage
        self.validation_error_category = validation_error_category
        self.adapter_resolution_status = adapter_resolution_status
        self.field_errors = list(field_errors or [])[:30]
        self.source_transport_started = False
        self.retryable = False


class SourceHttpRequest(StrictContract):
    url: str
    params: dict[str, str | int]
    accepted_mime_types: list[str]
    allow_official_geo_text: bool = False
    required_credential: Literal["epa_comptox_api_key"] | None = None
    allow_empty_health_response: bool = False
    accept_header: Literal["text/html"] | None = None
    approved_request_hosts: list[str] = Field(default_factory=list, max_length=10)
    approved_redirect_hosts: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("approved_request_hosts", "approved_redirect_hosts")
    @classmethod
    def redirect_hosts_are_exact_hosts(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                value != value.casefold().rstrip(".")
                or "://" in value
                or "/" in value
                or "@" in value
            ):
                raise ValueError("approved redirect hosts must be normalized exact host names")
        return values

    @model_validator(mode="after")
    def no_credentials_or_model_urls(self) -> SourceHttpRequest:
        if "@" in self.url or not self.url.startswith("https://"):
            raise ValueError("reviewed requests require credential-free HTTPS")
        return self


class ReviewedSourceExecutionTelemetry(StrictContract):
    """Safe, body-free telemetry for an isolated or production adapter execution."""

    adapter_id: str
    adapter_version: str
    source_system: str
    operation_name: str
    initial_host: str | None = None
    http_method: Literal["GET"] = "GET"
    final_allowlisted_host: str | None = None
    http_status: int | None = None
    redirect_count: int = Field(default=0, ge=0, le=3)
    mime_type: str | None = None
    response_bytes: int | None = Field(default=None, ge=0)
    transport_duration_ms: int = Field(default=0, ge=0)
    parse_duration_ms: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    parser_status: Literal["succeeded", "failed", "not_run"]
    cache_write_status: Literal["written", "not_written", "failed", "not_applicable"]
    cache_status: Literal["miss_written", "hit", "miss_not_written"]
    cache_key: str
    raw_artifact_id: str | None = None
    raw_artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provenance_record_id: str | None = None
    retry_count: int = Field(default=0, ge=0, le=2)
    sanitized_diagnostic: SourceToolDiagnostic | None = None
    validation_stage_reached: str = Field(default="not_started", max_length=120)
    parser_stage_reached: str = Field(default="not_started", max_length=120)
    failure_category: str | None = Field(default=None, max_length=120)
    sanitized_message: str | None = Field(default=None, max_length=500)
    retryable: bool = False
    transport_attempts: list[dict[str, Any]] = Field(default_factory=list, max_length=3)


class ReviewedSourceExecutionResult(StrictContract):
    batch: VerifiedSourceObservationBatch
    telemetry: ReviewedSourceExecutionTelemetry


class ReviewedSourceExecutionError(RuntimeError):
    retryable = False

    def __init__(self, safe_message: str, telemetry: ReviewedSourceExecutionTelemetry) -> None:
        super().__init__(safe_message)
        self.telemetry = telemetry


def _definition(
    *,
    adapter_id: str,
    system: str,
    roles: list[ComponentRole],
    operations: list[str],
    domains: list[str],
    parser: str,
    accepted_mime_types: list[str] | None = None,
    request_timeout_seconds: float = 15,
    source_retry_count: int = 0,
    maximum_response_bytes: int = 500_000,
) -> ReviewedSourceAdapterDefinition:
    return ReviewedSourceAdapterDefinition(
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        official_source_system=system,
        supported_component_roles=roles,
        approved_operations=operations,
        allowlisted_domains=domains,
        request_builder=f"{adapter_id}.build_request.v1",
        typed_response_parser=parser,
        accepted_mime_types=accepted_mime_types or sorted(ALLOWED_CONTENT_TYPES),
        maximum_response_bytes=maximum_response_bytes,
        request_timeout_seconds=request_timeout_seconds,
        maximum_redirects=2,
        source_retry_count=source_retry_count,
        requests_per_second=2,
        cache_ttl_seconds=86_400,
        provenance_format="adapter/version + immutable response artifact SHA-256",
        health_probe_capable=True,
        review_status=AdapterReviewStatus.APPROVED,
    )


PUBCHEM_BIOASSAY_ADAPTER = _definition(
    adapter_id="pubchem-bioassay",
    system="PubChem BioAssay",
    roles=[
        ComponentRole.ENDPOINT_ACTIVITY,
        ComponentRole.ASSAY_METADATA,
        ComponentRole.COUNTER_SCREEN,
    ],
    operations=[
        "search_activity_sources",
        "validate_activity_source",
        "fetch_activity_source_metadata",
        "inspect_activity_result_availability",
        "inspect_activity_identifier_fields",
        "extract_activity_result_rows",
        "summarize_activity_outcomes",
        "inspect_counter_screen_relationships",
    ],
    domains=["eutils.ncbi.nlm.nih.gov", "pubchem.ncbi.nlm.nih.gov"],
    parser="pubchem_bioassay_json_or_csv_v2",
    accepted_mime_types=[*sorted(ALLOWED_CONTENT_TYPES), "text/csv", "application/csv"],
    request_timeout_seconds=180,
    source_retry_count=1,
    maximum_response_bytes=5_000_000,
)

NCBI_GEO_ADAPTER = _definition(
    adapter_id="ncbi-geo-series",
    system="NCBI GEO Series",
    roles=[
        ComponentRole.TRANSCRIPTOMIC_MATRIX,
        ComponentRole.TRANSCRIPTOMIC_CONDITIONS,
        ComponentRole.SAMPLE_METADATA,
        ComponentRole.SOURCE_ID_MAPPING,
    ],
    operations=[
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
    domains=["eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"],
    parser="ncbi_geo_json_or_accession_text_v1",
    request_timeout_seconds=180,
    source_retry_count=1,
)

EPA_COMPTOX_TOXCAST_ADAPTER = _definition(
    adapter_id="epa-comptox-toxcast",
    system="EPA CompTox / ToxCast",
    roles=[
        ComponentRole.ENDPOINT_ACTIVITY,
        ComponentRole.ASSAY_METADATA,
        ComponentRole.COUNTER_SCREEN,
        ComponentRole.SOURCE_ID_MAPPING,
        ComponentRole.PROVENANCE_LICENSE,
    ],
    operations=[
        "check_epa_bioactivity_health",
        "search_epa_assays",
        "inspect_epa_assay_metadata",
        "inspect_epa_activity_availability",
        "inspect_epa_compound_identifier_fields",
        "inspect_epa_release_manifest",
        "inspect_epa_related_assay_components",
    ],
    domains=["comptox.epa.gov"],
    parser="epa_comptox_toxcast_assay_json_v1",
)

EPA_TOXCAST_PUBLIC_DOWNLOADS_ADAPTER = _definition(
    adapter_id="epa-toxcast-public-downloads",
    system="EPA ToxCast public data releases",
    roles=[
        ComponentRole.ENDPOINT_ACTIVITY,
        ComponentRole.ASSAY_METADATA,
        ComponentRole.SOURCE_ID_MAPPING,
        ComponentRole.PROVENANCE_LICENSE,
    ],
    operations=[
        "inspect_epa_public_invitrodb_release",
        "inspect_epa_public_database_package",
        "inspect_epa_public_assay_annotations",
        "inspect_epa_public_assay_target_mapping",
        "inspect_epa_public_summary_files",
        "inspect_epa_public_chemical_archive",
    ],
    domains=sorted(EPA_PUBLIC_DISTRIBUTION_HOSTS),
    parser="epa_toxcast_public_release_manifest_html_v1",
    accepted_mime_types=["text/html"],
)

LINCS_L1000_ADAPTER = _definition(
    adapter_id="lincs-l1000",
    system="LINCS L1000",
    roles=[
        ComponentRole.TRANSCRIPTOMIC_MATRIX,
        ComponentRole.TRANSCRIPTOMIC_CONDITIONS,
        ComponentRole.SAMPLE_METADATA,
        ComponentRole.COMPOUND_IDENTITY,
        ComponentRole.SOURCE_ID_MAPPING,
        ComponentRole.PROVENANCE_LICENSE,
    ],
    operations=[
        "check_lincs_geo_distribution_health",
        "search_lincs_resources",
        "inspect_lincs_perturbagen_catalogue",
        "inspect_lincs_signature_metadata",
        "inspect_lincs_feature_space",
        "inspect_lincs_processed_signature_availability",
        "inspect_lincs_release_manifest",
    ],
    domains=[
        "eutils.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "ftp.ncbi.nlm.nih.gov",
        "api.clue.io",
        "clue.io",
        "lincsproject.org",
    ],
    parser="lincs_l1000_geo_json_or_accession_text_v1",
    request_timeout_seconds=180,
    source_retry_count=1,
)

PUBCHEM_COMPOUND_ADAPTER = _definition(
    adapter_id="pubchem-compound",
    system="PubChem Compound",
    roles=[
        ComponentRole.COMPOUND_IDENTITY,
        ComponentRole.CHEMICAL_STRUCTURE,
        ComponentRole.SOURCE_ID_MAPPING,
    ],
    operations=[
        "inspect_source_identity_fields",
        "inspect_source_record_availability",
        "resolve_compound_identity_sample",
        "resolve_compound_synonyms",
    ],
    domains=["pubchem.ncbi.nlm.nih.gov"],
    parser="pubchem_compound_properties_json_v1",
    request_timeout_seconds=180,
    source_retry_count=1,
)

NCBI_SUPPORTING_METADATA_ADAPTER = _definition(
    adapter_id="ncbi-supporting-metadata",
    system="NCBI linked metadata",
    roles=[ComponentRole.PROVENANCE_LICENSE, ComponentRole.VALIDATION_REFERENCE],
    operations=[
        "inspect_supporting_metadata",
        "inspect_official_file_listing",
        "inspect_linked_publications",
        "inspect_source_access",
    ],
    domains=["eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"],
    parser="ncbi_linked_metadata_json_v1",
    request_timeout_seconds=180,
    source_retry_count=1,
)

REVIEWED_ADAPTER_DEFINITIONS = (
    PUBCHEM_BIOASSAY_ADAPTER,
    EPA_TOXCAST_PUBLIC_DOWNLOADS_ADAPTER,
    EPA_COMPTOX_TOXCAST_ADAPTER,
    NCBI_GEO_ADAPTER,
    LINCS_L1000_ADAPTER,
    PUBCHEM_COMPOUND_ADAPTER,
    NCBI_SUPPORTING_METADATA_ADAPTER,
)


class ReviewedSourceAdapter:
    """Production adapter with injected bounded transport, cache and artifact store."""

    def __init__(
        self,
        definition: ReviewedSourceAdapterDefinition,
        client: ScientificSourceClient,
        cache: SourceResponseCache,
        artifacts: LocalArtifactStore,
        *,
        epa_comptox_api_key: str | None = None,
    ) -> None:
        self.definition = definition
        self.client = client
        self.cache = cache
        self.artifacts = artifacts
        self._epa_comptox_api_key = epa_comptox_api_key

    def execute(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> VerifiedSourceObservationBatch:
        return self.execute_with_telemetry(operation, request, invocation).batch

    def operation_runtime_status(
        self, operation: str
    ) -> Literal["ready", "authenticated_api_unavailable", "unapproved_operation"]:
        if operation not in self.definition.approved_operations:
            return "unapproved_operation"
        if (
            self.definition.adapter_id == "epa-comptox-toxcast"
            and operation in EPA_AUTHENTICATED_OPERATIONS
            and not self._epa_comptox_api_key
        ):
            return "authenticated_api_unavailable"
        return "ready"

    def execute_with_telemetry(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> ReviewedSourceExecutionResult:
        total_started = time.monotonic()
        if operation not in self.definition.approved_operations:
            raise ValueError("operation is not approved for this reviewed adapter")
        if invocation.workflow_id is None or invocation.step_id is None:
            raise ValueError("source tools require durable workflow and step identifiers")
        arguments = request.model_dump(mode="json")
        cache_tool_name = f"{self.definition.adapter_id}:{operation}"
        cache_key = self.cache.key(
            cache_tool_name,
            arguments,
            source_version=self.definition.adapter_version,
        )
        cached = self.cache.get(
            cache_tool_name,
            arguments,
            source_version=self.definition.adapter_version,
        )
        if cached is not None:
            batch = VerifiedSourceObservationBatch.model_validate(cached.parsed_output).model_copy(
                update={"cache_status": "cached", "source_request_count": 0}
            )
            if batch.search_outcome is not None:
                batch = batch.model_copy(
                    update={
                        "search_outcome": batch.search_outcome.model_copy(
                            update={"cache_status": "cached"}
                        )
                    }
                )
            stored = cached.http_metadata.get("execution_telemetry", {})
            telemetry = ReviewedSourceExecutionTelemetry.model_validate(
                {
                    "adapter_id": self.definition.adapter_id,
                    "adapter_version": self.definition.adapter_version,
                    "source_system": self.definition.official_source_system,
                    "operation_name": operation,
                    "initial_host": stored.get("initial_host"),
                    "final_allowlisted_host": stored.get("final_allowlisted_host"),
                    "http_status": stored.get("http_status"),
                    "redirect_count": stored.get("redirect_count", 0),
                    "mime_type": stored.get("mime_type"),
                    "response_bytes": stored.get("response_bytes"),
                    "transport_duration_ms": stored.get("transport_duration_ms", 0),
                    "parse_duration_ms": stored.get("parse_duration_ms", 0),
                    "total_duration_ms": max(0, int((time.monotonic() - total_started) * 1000)),
                    "parser_status": "succeeded",
                    "cache_write_status": "not_applicable",
                    "cache_status": "hit",
                    "cache_key": cached.cache_key,
                    "raw_artifact_id": cached.raw_artifact_id,
                    "raw_artifact_sha256": cached.content_hash,
                    "provenance_record_id": f"source-cache:{cached.cache_key}",
                    "retry_count": stored.get("retry_count", 0),
                    "sanitized_diagnostic": stored.get("sanitized_diagnostic"),
                    "validation_stage_reached": stored.get("validation_stage_reached", "complete"),
                    "parser_stage_reached": stored.get("parser_stage_reached", "complete"),
                    "failure_category": stored.get("failure_category"),
                    "sanitized_message": stored.get("sanitized_message"),
                    "retryable": stored.get("retryable", False),
                    "transport_attempts": stored.get("transport_attempts", []),
                }
            )
            return ReviewedSourceExecutionResult(batch=batch, telemetry=telemetry)
        try:
            source_request = self._build_request(operation, request)
        except ValueError as exc:
            raise ReviewedSourceInvocationError(
                str(exc),
                invocation_stage="request_construction",
                validation_error_category="required_argument_missing_or_invalid",
                adapter_resolution_status=f"resolved:{self.definition.adapter_id}",
                field_errors=[
                    {
                        "field": "operation_arguments",
                        "category": "request_construction_rejected",
                        "message": str(exc)[:300],
                    }
                ],
            ) from exc
        request_host = (urlparse(source_request.url).hostname or "").lower().rstrip(".")
        if request_host not in self.definition.allowlisted_domains:
            raise ValueError("reviewed adapter request host is outside its definition allowlist")
        try:
            response = self.client.get(
                source_request.url,
                tool_name=operation,
                params=source_request.params,
                accepted_types=frozenset(source_request.accepted_mime_types),
                allow_official_geo_text=source_request.allow_official_geo_text,
                maximum_bytes=self.definition.maximum_response_bytes,
                headers=self._request_headers(source_request),
                allow_empty_health_response=source_request.allow_empty_health_response,
                approved_request_hosts=(
                    frozenset(source_request.approved_request_hosts)
                    if source_request.approved_request_hosts
                    else None
                ),
                approved_redirect_hosts=(
                    frozenset(source_request.approved_redirect_hosts)
                    if source_request.approved_redirect_hosts
                    else None
                ),
                maximum_attempts=min(
                    1 + self.definition.source_retry_count,
                    self.client.maximum_attempts,
                ),
            )
        except SourceToolError as exc:
            diagnostic = exc.diagnostic
            category = (
                diagnostic.source_error_category
                if diagnostic is not None
                else "source_transport_failure"
            )
            validation_stage = {
                "source_client_error": "http_status_validation",
                "rate_limited": "http_status_validation",
                "source_server_error": "http_status_validation",
                "invalid_redirect": "redirect_validation",
                "redirect_not_approved": "redirect_validation",
                "redirect_limit_exceeded": "redirect_validation",
                "unexpected_content_type": "mime_validation",
                "response_too_large": "response_size_validation",
                "source_url_policy": "request_url_validation",
                "timeout": "transport",
                "network_unavailable": "transport",
            }.get(category, "transport")
            telemetry = self._failure_telemetry(
                operation=operation,
                cache_key=cache_key,
                initial_host=request_host,
                total_started=total_started,
                diagnostic=diagnostic,
                validation_stage=validation_stage,
                parser_stage="not_started",
                failure_category=category,
                sanitized_message=str(exc),
                retryable=bool(getattr(exc, "retryable", False)),
                transport_attempts=list(getattr(exc, "attempt_diagnostics", ())),
            )
            raise ReviewedSourceExecutionError(str(exc), telemetry) from exc
        try:
            artifact = self.artifacts.put_bytes(
                workflow_id=invocation.workflow_id,
                step_id=invocation.step_id,
                content=response.content,
                mime_type="application/octet-stream",
                artifact_type="immutable_source_response",
                logical_name=(
                    f"source-response-{self.definition.adapter_id}-{operation}-"
                    f"{response.sha256[:16]}"
                ),
                producer=f"{self.definition.adapter_id}@{self.definition.adapter_version}",
                original_source=response.url,
                idempotency_key=invocation.idempotency_key or response.sha256,
            )
        except Exception as exc:
            telemetry = self._failure_telemetry(
                operation=operation,
                cache_key=cache_key,
                initial_host=request_host,
                total_started=total_started,
                diagnostic=response.diagnostic,
                validation_stage="response_validated",
                parser_stage="not_started",
                failure_category="artifact_persistence",
                sanitized_message="Reviewed source artifact could not be persisted.",
                retryable=False,
                response=response,
            )
            raise ReviewedSourceExecutionError(
                "Reviewed source artifact could not be persisted.", telemetry
            ) from exc
        parse_started = time.monotonic()
        try:
            observations = self._parse_response(
                operation,
                request,
                response.content,
                response.content_type,
                response.url,
                artifact.id,
                artifact.sha256,
            )
            activity_rows, inspected_activity_count, excluded_activity_count = (
                self._activity_result_rows(
                    operation,
                    request,
                    response.content,
                    response.url,
                    artifact.id,
                    artifact.sha256,
                )
            )
            identity_bridge_rows = self._compound_identity_rows(
                operation,
                response.content,
                response.url,
                artifact.id,
                artifact.sha256,
            )
            transcriptomic_profile_rows = self._transcriptomic_profile_rows(
                operation,
                request,
                response.content,
                response.url,
                artifact.id,
                artifact.sha256,
            )
        except Exception as exc:
            failure_category = getattr(exc, "category", "parser_rejected")
            parser_stage = getattr(exc, "stage", "failed")
            sanitized_message = getattr(
                exc,
                "safe_message",
                "Reviewed source parser rejected the bounded response.",
            )
            diagnostic = (
                response.diagnostic.model_copy(
                    update={
                        "parser_outcome": parser_stage,
                        "source_artifact_id": artifact.id,
                        "source_error_category": failure_category,
                        "developer_message": sanitized_message,
                    }
                )
                if response.diagnostic
                else None
            )
            telemetry = self._telemetry(
                operation=operation,
                cache_key=cache_key,
                initial_host=request_host,
                response=response,
                artifact_id=artifact.id,
                artifact_sha256=artifact.sha256,
                parse_started=parse_started,
                total_started=total_started,
                parser_status="failed",
                cache_write_status="not_written",
                cache_status="miss_not_written",
                diagnostic=diagnostic,
                failure_category=failure_category,
                sanitized_message=sanitized_message,
                parser_stage_reached=parser_stage,
            )
            raise ReviewedSourceExecutionError(
                "Reviewed source parser rejected the bounded response.", telemetry
            ) from exc
        search_outcome = None
        if operation.startswith("search_"):
            search_outcome = VerifiedSourceSearchOutcome(
                outcome_id=deterministic_id(
                    "search-outcome",
                    self.definition.adapter_id,
                    operation,
                    artifact.sha256,
                    response.url,
                ),
                adapter_id=self.definition.adapter_id,
                adapter_version=self.definition.adapter_version,
                source_system=self.definition.official_source_system,
                operation=operation,
                rendered_query=response.url,
                query_scope={
                    key: value
                    for key, value in {
                        "biological_target": request.biological_target,
                        "endpoint_modality": request.endpoint_modality,
                        "query": request.query,
                        "sampled_identifiers": request.sampled_identifiers,
                        "verified_compound_names": request.verified_compound_names,
                        "maximum_results": request.maximum_results,
                    }.items()
                    if value is not None
                },
                result_count=len(observations),
                outcome=(
                    "completed_with_candidates" if observations else "completed_no_candidates"
                ),
                http_status=response.status_code,
                cache_status="live",
                source_request_artifact_ids=[artifact.id],
            )
        batch = VerifiedSourceObservationBatch(
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            operation=operation,
            cache_status="live",
            observations=observations,
            source_request_artifact_ids=[artifact.id],
            source_request_count=max(1, len(response.attempt_diagnostics)),
            search_outcome=search_outcome,
            activity_rows=activity_rows,
            identity_bridge_rows=identity_bridge_rows,
            transcriptomic_profile_rows=transcriptomic_profile_rows,
            inspected_record_count=(
                inspected_activity_count
                if operation == "extract_activity_result_rows"
                else len(transcriptomic_profile_rows)
            ),
            excluded_record_count=(
                excluded_activity_count
                + sum(row.exclusion_reason is not None for row in transcriptomic_profile_rows)
            ),
        )
        diagnostic = (
            response.diagnostic.model_copy(
                update={"parser_outcome": "succeeded", "source_artifact_id": artifact.id}
            )
            if response.diagnostic
            else None
        )
        telemetry = self._telemetry(
            operation=operation,
            cache_key=cache_key,
            initial_host=request_host,
            response=response,
            artifact_id=artifact.id,
            artifact_sha256=artifact.sha256,
            parse_started=parse_started,
            total_started=total_started,
            parser_status="succeeded",
            cache_write_status="written",
            cache_status="miss_written",
            diagnostic=diagnostic,
        )
        batch = batch.model_copy(update={"transport_attempts": telemetry.transport_attempts})
        try:
            self.cache.put(
                cache_tool_name,
                arguments,
                source_url=response.url,
                content_hash=response.sha256,
                parsed_output=batch.model_dump(mode="json"),
                raw_artifact_id=artifact.id,
                http_metadata={
                    "adapter_id": self.definition.adapter_id,
                    "review_policy_version": self.definition.review_policy_version,
                    "execution_telemetry": telemetry.model_dump(mode="json"),
                },
                source_version=self.definition.adapter_version,
            )
        except Exception as exc:
            failed = telemetry.model_copy(
                update={
                    "cache_write_status": "failed",
                    "cache_status": "miss_not_written",
                    "provenance_record_id": None,
                    "total_duration_ms": max(0, int((time.monotonic() - total_started) * 1000)),
                    "validation_stage_reached": "response_validated",
                    "parser_stage_reached": "complete",
                    "failure_category": "cache_persistence",
                    "sanitized_message": (
                        "Reviewed source cache could not persist artifact provenance."
                    ),
                }
            )
            raise ReviewedSourceExecutionError(
                "Reviewed source cache could not persist artifact provenance.", failed
            ) from exc
        return ReviewedSourceExecutionResult(batch=batch, telemetry=telemetry)

    def _telemetry(
        self,
        *,
        operation: str,
        cache_key: str,
        initial_host: str,
        response: Any,
        artifact_id: str,
        artifact_sha256: str,
        parse_started: float,
        total_started: float,
        parser_status: Literal["succeeded", "failed", "not_run"],
        cache_write_status: Literal["written", "not_written", "failed", "not_applicable"],
        cache_status: Literal["miss_written", "hit", "miss_not_written"],
        diagnostic: SourceToolDiagnostic | None,
        failure_category: str | None = None,
        sanitized_message: str | None = None,
        parser_stage_reached: str | None = None,
    ) -> ReviewedSourceExecutionTelemetry:
        return ReviewedSourceExecutionTelemetry(
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            source_system=self.definition.official_source_system,
            operation_name=operation,
            initial_host=initial_host,
            final_allowlisted_host=(diagnostic.final_approved_host if diagnostic else None),
            http_status=response.status_code,
            redirect_count=(diagnostic.redirect_count if diagnostic else 0),
            mime_type=response.content_type,
            response_bytes=len(response.content),
            transport_duration_ms=(diagnostic.request_duration_ms if diagnostic else 0),
            parse_duration_ms=max(0, int((time.monotonic() - parse_started) * 1000)),
            total_duration_ms=max(0, int((time.monotonic() - total_started) * 1000)),
            parser_status=parser_status,
            cache_write_status=cache_write_status,
            cache_status=cache_status,
            cache_key=cache_key,
            raw_artifact_id=artifact_id,
            raw_artifact_sha256=artifact_sha256,
            provenance_record_id=(
                f"source-cache:{cache_key}" if cache_write_status == "written" else None
            ),
            retry_count=max(0, len(response.attempt_diagnostics) - 1),
            sanitized_diagnostic=diagnostic,
            validation_stage_reached="response_validated",
            parser_stage_reached=(
                parser_stage_reached or ("complete" if parser_status == "succeeded" else "failed")
            ),
            failure_category=failure_category,
            sanitized_message=sanitized_message,
            retryable=False,
            transport_attempts=[
                item.model_dump(mode="json") for item in response.attempt_diagnostics
            ],
        )

    def _failure_telemetry(
        self,
        *,
        operation: str,
        cache_key: str,
        initial_host: str,
        total_started: float,
        diagnostic: SourceToolDiagnostic | None,
        validation_stage: str,
        parser_stage: str,
        failure_category: str,
        sanitized_message: str,
        retryable: bool,
        response: Any | None = None,
        transport_attempts: list[SourceToolDiagnostic] | None = None,
    ) -> ReviewedSourceExecutionTelemetry:
        return ReviewedSourceExecutionTelemetry(
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            source_system=self.definition.official_source_system,
            operation_name=operation,
            initial_host=initial_host,
            final_allowlisted_host=(diagnostic.final_approved_host if diagnostic else None),
            http_status=(
                response.status_code
                if response is not None
                else (diagnostic.http_status if diagnostic else None)
            ),
            redirect_count=(diagnostic.redirect_count if diagnostic else 0),
            mime_type=(
                response.content_type
                if response is not None
                else (diagnostic.content_type if diagnostic else None)
            ),
            response_bytes=(
                len(response.content)
                if response is not None
                else (diagnostic.response_byte_count if diagnostic else None)
            ),
            transport_duration_ms=(diagnostic.request_duration_ms if diagnostic else 0),
            parse_duration_ms=0,
            total_duration_ms=max(0, int((time.monotonic() - total_started) * 1000)),
            parser_status="not_run" if parser_stage == "not_started" else "failed",
            cache_write_status="not_written",
            cache_status="miss_not_written",
            cache_key=cache_key,
            retry_count=max(0, len(transport_attempts or []) - 1),
            sanitized_diagnostic=diagnostic,
            validation_stage_reached=validation_stage,
            parser_stage_reached=parser_stage,
            failure_category=failure_category,
            sanitized_message=sanitized_message,
            retryable=retryable,
            transport_attempts=[
                item.model_dump(mode="json") for item in (transport_attempts or [])
            ],
        )

    def _build_request(
        self, operation: str, request: ReviewedSourceOperationInput
    ) -> SourceHttpRequest:
        query = request.query or " ".join(
            item for item in (request.biological_target, request.endpoint_modality) if item
        )
        if self.definition.adapter_id == "epa-comptox-toxcast":
            if operation == "check_epa_bioactivity_health":
                return SourceHttpRequest(
                    url="https://comptox.epa.gov/ctx-api/bioactivity/health",
                    params={},
                    accepted_mime_types=[
                        "application/json",
                        "application/vnd.spring-boot.actuator.v2+json",
                        "application/vnd.spring-boot.actuator.v3+json",
                        TECHNICAL_HEALTH_STATUS_MIME,
                        "text/plain",
                    ],
                    allow_empty_health_response=True,
                )
            search_term = query or request.stable_identifier
            if not search_term:
                raise ValueError("EPA assay operations require a typed query or stable identifier")
            if operation == "search_epa_assays":
                return SourceHttpRequest(
                    url="https://comptox.epa.gov/ctx-api/bioactivity/assay/",
                    params={},
                    accepted_mime_types=["application/json"],
                    required_credential="epa_comptox_api_key",
                )
            if operation == "inspect_epa_release_manifest":
                raise ValueError(
                    "EPA release authority is exposed only by the reviewed public "
                    "invitrodb release adapter, not the CTX scientific API."
                )
            stable_identifier = self._required_identifier(request)
            aeid = stable_identifier.removeprefix("AEID:").removeprefix("aeid:")
            if not re.fullmatch(r"[1-9][0-9]{0,9}", aeid):
                raise ValueError("EPA metadata operations require a numeric AEID")
            if operation == "inspect_epa_activity_availability":
                return SourceHttpRequest(
                    url=(
                        "https://comptox.epa.gov/ctx-api/bioactivity/data/summary/"
                        f"search/by-aeid/{aeid}"
                    ),
                    params={},
                    accepted_mime_types=["application/json"],
                    required_credential="epa_comptox_api_key",
                )
            if operation == "inspect_epa_compound_identifier_fields":
                return SourceHttpRequest(
                    url=(
                        "https://comptox.epa.gov/ctx-api/bioactivity/data/" f"search/by-aeid/{aeid}"
                    ),
                    params={},
                    accepted_mime_types=["application/json"],
                    required_credential="epa_comptox_api_key",
                )
            return SourceHttpRequest(
                url=("https://comptox.epa.gov/ctx-api/bioactivity/assay/search/by-aeid/" f"{aeid}"),
                params={},
                accepted_mime_types=["application/json"],
                required_credential="epa_comptox_api_key",
            )
        if self.definition.adapter_id == "epa-toxcast-public-downloads":
            return SourceHttpRequest(
                url=EPA_TOXCAST_PUBLIC_RELEASE_PAGE,
                params={},
                accepted_mime_types=["text/html"],
                accept_header="text/html",
                approved_request_hosts=["clowder.edap-cluster.com"],
                approved_redirect_hosts=sorted(EPA_PUBLIC_DISTRIBUTION_HOSTS),
            )
        if self.definition.adapter_id == "lincs-l1000":
            if operation == "check_lincs_geo_distribution_health":
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
                    params={"db": "gds", "retmode": "json"},
                    accepted_mime_types=["application/json", "text/plain"],
                )
            if operation == "search_lincs_resources":
                if not query:
                    raise ValueError("LINCS resource search requires a typed query")
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={
                        "db": "gds",
                        "term": f"LINCS L1000 {query} AND gse[ETYP]",
                        "retmode": "json",
                        "retmax": request.maximum_results,
                    },
                    accepted_mime_types=["application/json", "text/plain"],
                )
            stable = self._required_identifier(request).upper()
            if stable.startswith("GDS_UID:"):
                uid = stable.removeprefix("GDS_UID:")
                if not re.fullmatch(r"[1-9][0-9]{0,11}", uid):
                    raise ValueError("LINCS metadata requires a numeric GDS UID")
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    params={"db": "gds", "id": uid, "retmode": "json"},
                    accepted_mime_types=["application/json", "text/plain"],
                )
            if not re.fullmatch(r"GSE[1-9][0-9]{1,8}", stable):
                raise ValueError(
                    "LINCS metadata operations require a validated GSE accession or GDS UID"
                )
            return SourceHttpRequest(
                url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi",
                params={"acc": stable, "targ": "self", "view": "full", "form": "text"},
                accepted_mime_types=["text/plain", "geo/text"],
                allow_official_geo_text=True,
            )
        if self.definition.adapter_id == "pubchem-bioassay":
            if operation == "search_activity_sources":
                if not query:
                    raise ValueError("activity search requires a typed target query")
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={
                        "db": "pcassay",
                        "term": query,
                        "retmode": "json",
                        "retmax": request.maximum_results,
                    },
                    accepted_mime_types=["application/json", "text/plain"],
                )
            stable = self._required_identifier(request)
            aid = stable.removeprefix("AID:").removeprefix("aid:")
            if not re.fullmatch(r"[1-9][0-9]{0,11}", aid):
                raise ValueError("PubChem BioAssay metadata requires a numeric AID")
            if operation == "inspect_activity_identifier_fields":
                return SourceHttpRequest(
                    url=("https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/aid/" f"{aid}/cids/JSON"),
                    params={"cids_type": "active"},
                    accepted_mime_types=["application/json"],
                )
            if operation == "extract_activity_result_rows":
                return SourceHttpRequest(
                    url=("https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/aid/" f"{aid}/CSV"),
                    params={"response_type": "display"},
                    accepted_mime_types=["text/csv", "application/csv", "text/plain"],
                )
            return SourceHttpRequest(
                url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "pcassay", "id": aid, "retmode": "json"},
                accepted_mime_types=["application/json", "text/plain"],
            )
        if self.definition.adapter_id == "ncbi-geo-series":
            if operation == "search_transcriptomic_sources":
                if not query:
                    raise ValueError("transcriptomic search requires a typed query")
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={
                        "db": "gds",
                        "term": f"{query} AND gse[ETYP]",
                        "retmode": "json",
                        "retmax": request.maximum_results,
                    },
                    accepted_mime_types=["application/json", "text/plain"],
                )
            stable = self._required_identifier(request).upper()
            if stable.startswith("GDS_UID:"):
                uid = stable.removeprefix("GDS_UID:")
                if not re.fullmatch(r"[1-9][0-9]{0,11}", uid):
                    raise ValueError("GEO metadata requires a numeric GDS UID")
                return SourceHttpRequest(
                    url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    params={"db": "gds", "id": uid, "retmode": "json"},
                    accepted_mime_types=["application/json", "text/plain"],
                )
            if not re.fullmatch(r"GSE[1-9][0-9]{1,8}", stable):
                raise ValueError("GEO operations require a validated GSE accession")
            return SourceHttpRequest(
                url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi",
                params={"acc": stable, "targ": "self", "view": "full", "form": "text"},
                accepted_mime_types=["text/plain", "geo/text"],
                allow_official_geo_text=True,
            )
        if self.definition.adapter_id == "pubchem-compound":
            identifiers = request.sampled_identifiers or (
                [request.stable_identifier] if request.stable_identifier else []
            )
            if not identifiers or len(identifiers) > 10:
                raise ValueError("compound inspection requires one to ten sampled identifiers")
            requested_namespace = (request.identifier_type or "cid").casefold()
            namespace = {
                "pubchem cid": "cid",
                "cid": "cid",
                "inchikey": "inchikey",
                "compound name": "name",
                "name": "name",
            }.get(requested_namespace)
            if namespace is None:
                raise ValueError("compound identifier type is not approved")
            joined = ",".join(quote(item, safe="") for item in identifiers)
            suffix = (
                "synonyms/JSON"
                if operation == "resolve_compound_synonyms"
                else "property/Title,CanonicalSMILES,IsomericSMILES,InChIKey/JSON"
            )
            return SourceHttpRequest(
                url=(
                    f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{namespace}/{joined}/"
                    f"{suffix}"
                ),
                params={},
                accepted_mime_types=["application/json"],
            )
        identifiers = request.source_identifiers or (
            [request.stable_identifier] if request.stable_identifier else []
        )
        if not identifiers:
            raise ValueError("supporting metadata inspection requires stable source identifiers")
        first_identifier = identifiers[0].upper()
        if first_identifier.startswith("GDS_UID:"):
            uid = first_identifier.removeprefix("GDS_UID:")
            if not re.fullmatch(r"[1-9][0-9]{0,11}", uid):
                raise ValueError("supporting GEO metadata requires a numeric GDS UID")
            return SourceHttpRequest(
                url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "gds", "id": uid, "retmode": "json"},
                accepted_mime_types=["application/json", "text/plain"],
            )
        if re.fullmatch(r"GSE[1-9][0-9]{1,8}", first_identifier):
            return SourceHttpRequest(
                url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi",
                params={
                    "acc": first_identifier,
                    "targ": "self",
                    "view": "full",
                    "form": "text",
                },
                accepted_mime_types=["text/plain", "geo/text"],
                allow_official_geo_text=True,
            )
        return SourceHttpRequest(
            url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
            params={
                "dbfrom": "pcassay",
                "db": "pubmed",
                "id": ",".join(
                    item.removeprefix("AID:").removeprefix("aid:") for item in identifiers
                ),
                "retmode": "json",
            },
            accepted_mime_types=["application/json", "text/plain"],
        )

    @staticmethod
    def _required_identifier(request: ReviewedSourceOperationInput) -> str:
        if not request.stable_identifier:
            raise ValueError("operation requires a stable source identifier")
        return request.stable_identifier

    def _credential_headers(self, request: SourceHttpRequest) -> dict[str, str] | None:
        if request.required_credential is None:
            return None
        if request.required_credential != "epa_comptox_api_key":
            raise ValueError("reviewed source request uses an unsupported credential binding")
        if not self._epa_comptox_api_key:
            raise ValueError(
                "EPA CTX Bioactivity operations require EPA_COMPTOX_API_KEY; "
                "the unauthenticated health operation remains available."
            )
        return {"x-api-key": self._epa_comptox_api_key}

    def _request_headers(self, request: SourceHttpRequest) -> dict[str, str] | None:
        headers = dict(self._credential_headers(request) or {})
        if request.accept_header is not None:
            headers["Accept"] = request.accept_header
        return headers or None

    def _parse_response(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        content: bytes,
        content_type: str,
        source_url: str,
        artifact_id: str,
        artifact_hash: str,
    ) -> list[VerifiedSourceObservation]:
        records = self._normalized_records(
            operation,
            request,
            content,
            content_type,
            source_url=source_url,
        )
        return [
            self._observation(
                operation,
                record,
                source_url=source_url,
                artifact_id=artifact_id,
                artifact_hash=artifact_hash,
            )
            for record in records[: request.maximum_results]
        ]

    def _activity_result_rows(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        content: bytes,
        source_url: str,
        artifact_id: str,
        artifact_hash: str,
    ) -> tuple[list[ActivityEvidenceRow], int, int]:
        if operation != "extract_activity_result_rows":
            return [], 0, 0
        aid = str(request.stable_identifier or "").upper()
        modality_map = getattr(request, "modality_by_source", {})
        raw_modality = str(modality_map.get(aid) or modality_map.get(aid.casefold()) or "")
        if raw_modality not in {"binding", "agonism", "antagonism"}:
            raise ValueError("activity extraction requires a preserved assay modality")
        modality = cast(Literal["binding", "agonism", "antagonism"], raw_modality)
        maximum_rows = int(getattr(request, "maximum_rows_per_assay", 100))
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="strict")))
        if not reader.fieldnames or "PUBCHEM_CID" not in reader.fieldnames:
            raise ValueError("PubChem assay result CSV lacks the compound identifier column")
        standard = {
            "PUBCHEM_RESULT_TAG",
            "PUBCHEM_SID",
            "PUBCHEM_CID",
            "PUBCHEM_ACTIVITY_OUTCOME",
            "PUBCHEM_ACTIVITY_SCORE",
            "PUBCHEM_ACTIVITY_URL",
            "PUBCHEM_ASSAYDATA_COMMENT",
            "PUBCHEM_EXT_DATASOURCE_SMILES",
        }
        value_fields = [field for field in reader.fieldnames if field not in standard]
        rows: list[ActivityEvidenceRow] = []
        inspected_count = 0
        excluded_count = 0
        seen_records: set[tuple[str | None, str, str | None, str | None, str | None]] = set()
        for index, source_row in enumerate(reader, start=1):
            inspected_count += 1
            cid_raw = str(source_row.get("PUBCHEM_CID") or "").strip()
            sid_raw = str(source_row.get("PUBCHEM_SID") or "").strip()
            cid = f"CID:{cid_raw}" if re.fullmatch(r"[1-9][0-9]{0,11}", cid_raw) else None
            outcome = str(source_row.get("PUBCHEM_ACTIVITY_OUTCOME") or "").strip() or None
            selected_field = next(
                (field for field in value_fields if str(source_row.get(field) or "").strip()),
                None,
            )
            if (
                selected_field is None
                and str(source_row.get("PUBCHEM_ACTIVITY_SCORE") or "").strip()
            ):
                selected_field = "PUBCHEM_ACTIVITY_SCORE"
            activity_value = (
                str(source_row.get(selected_field) or "").strip() if selected_field else None
            ) or None
            unit_match = re.search(r"\[([^\]]+)\]|\(([^)]+)\)", selected_field or "")
            unit = next(
                (value for value in (unit_match.groups() if unit_match else ()) if value),
                None,
            )
            exclusion = None
            if cid is None:
                exclusion = "missing_or_invalid_pubchem_cid"
            elif outcome is None and activity_value is None:
                exclusion = "missing_source_activity_outcome_and_value"
            record_key = (cid, sid_raw, outcome, activity_value, selected_field)
            if exclusion is None and record_key in seen_records:
                exclusion = "duplicate_source_activity_record"
            seen_records.add(record_key)
            if exclusion:
                excluded_count += 1
            if len(rows) >= maximum_rows:
                continue
            original_fields = {
                key: str(value)[:500]
                for key, value in list(source_row.items())[:30]
                if value not in {None, ""}
            }
            rows.append(
                ActivityEvidenceRow(
                    row_id=deterministic_id(
                        "activity-row", aid, cid or sid_raw or str(index), modality, str(index)
                    ),
                    pubchem_aid=aid,
                    source_compound_identifier=(
                        f"SID:{sid_raw}" if sid_raw else cid or f"row:{index}"
                    ),
                    pubchem_cid=cid,
                    activity_outcome=outcome,
                    activity_value=activity_value,
                    activity_unit=unit,
                    activity_endpoint=selected_field,
                    assay_target=str(request.biological_target or "").strip() or None,
                    modality=modality,
                    source_locator=source_url,
                    raw_artifact_id=artifact_id,
                    raw_artifact_sha256=artifact_hash,
                    extraction_status="excluded" if exclusion else "included",
                    exclusion_reason=exclusion,
                    original_source_fields=original_fields,
                    evidence_references=[source_url],
                )
            )
        return rows, inspected_count, excluded_count

    @staticmethod
    def _compound_identity_rows(
        operation: str,
        content: bytes,
        source_url: str,
        artifact_id: str,
        artifact_hash: str,
    ) -> list[CompoundIdentityBridgeRow]:
        if operation not in {"resolve_compound_identity_sample", "resolve_compound_synonyms"}:
            return []
        payload = json.loads(content.decode("utf-8"))
        rows: list[CompoundIdentityBridgeRow] = []
        if operation == "resolve_compound_identity_sample":
            records = payload.get("PropertyTable", {}).get("Properties", [])
            for item in records:
                cid_raw = str(item.get("CID") or "")
                if not re.fullmatch(r"[1-9][0-9]{0,11}", cid_raw):
                    continue
                inchikey = str(item.get("InChIKey") or "").upper() or None
                status = cast(
                    Literal["exact", "missing"],
                    "exact"
                    if inchikey and re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", inchikey)
                    else "missing",
                )
                rows.append(
                    CompoundIdentityBridgeRow(
                        mapping_id=deterministic_id("identity-map", cid_raw, artifact_hash),
                        source_compound_identifier=f"CID:{cid_raw}",
                        pubchem_cid=f"CID:{cid_raw}",
                        inchikey=inchikey if status == "exact" else None,
                        canonical_name=str(item.get("Title") or "").strip() or None,
                        mapping_status=status,
                        raw_artifact_ids=[artifact_id],
                        raw_artifact_hashes=[artifact_hash],
                        evidence_references=[source_url],
                    )
                )
        else:
            records = payload.get("InformationList", {}).get("Information", [])
            for item in records:
                cid_raw = str(item.get("CID") or "")
                if not re.fullmatch(r"[1-9][0-9]{0,11}", cid_raw):
                    continue
                rows.append(
                    CompoundIdentityBridgeRow(
                        mapping_id=deterministic_id("identity-synonyms", cid_raw, artifact_hash),
                        source_compound_identifier=f"CID:{cid_raw}",
                        pubchem_cid=f"CID:{cid_raw}",
                        synonyms=[
                            str(value)[:500]
                            for value in item.get("Synonym", [])[:50]
                            if str(value).strip()
                        ],
                        mapping_status="missing",
                        raw_artifact_ids=[artifact_id],
                        raw_artifact_hashes=[artifact_hash],
                        evidence_references=[source_url],
                    )
                )
        return rows

    def _transcriptomic_profile_rows(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        content: bytes,
        source_url: str,
        artifact_id: str,
        artifact_hash: str,
    ) -> list[TranscriptomicProfileEvidenceRow]:
        if operation not in {
            "fetch_transcriptomic_source_metadata",
            "inspect_lincs_signature_metadata",
        }:
            return []
        cid = next(
            (
                value.upper()
                for value in request.sampled_identifiers
                if re.fullmatch(r"CID:[1-9][0-9]{0,11}", value, flags=re.I)
            ),
            None,
        )
        inchikey = next(
            (
                value.upper()
                for value in request.sampled_identifiers
                if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", value, flags=re.I)
            ),
            None,
        )
        verified_names = list(request.verified_compound_names) or [
            str(request.biological_target or "").strip()
        ]
        verified_names = [value for value in verified_names if value]
        canonical_name = verified_names[0] if verified_names else ""
        if cid is None or not canonical_name:
            return []
        payload = json.loads(content.decode("utf-8"))
        result = payload.get("result", {})
        rows: list[TranscriptomicProfileEvidenceRow] = []
        for uid in result.get("uids", []):
            item = result.get(str(uid), {})
            title = str(item.get("title") or "")
            summary = str(item.get("summary") or "")
            combined = f"{title} {summary}".casefold()
            applied = any(value.casefold() in combined for value in verified_names)
            accession = str(item.get("accession") or item.get("gse") or f"GDS_UID:{uid}").upper()
            locator = str(item.get("ftpLink") or item.get("suppfile") or "").strip()
            dose_match = re.search(r"\b\d+(?:\.\d+)?\s*(?:nM|uM|µM|mM|mg/L)\b", summary, re.I)
            time_match = re.search(r"\b\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours?|days?)\b", summary, re.I)
            exclusion = None
            if not applied:
                exclusion = "compound_application_not_verified_in_source_metadata"
            elif not locator:
                exclusion = "processed_profile_locator_unavailable"
            rows.append(
                TranscriptomicProfileEvidenceRow(
                    profile_row_id=deterministic_id(
                        "transcriptomic-profile", cid, accession, artifact_hash
                    ),
                    pubchem_cid=cid,
                    inchikey=inchikey,
                    canonical_name=canonical_name,
                    source_system=self.definition.official_source_system,
                    source_accession=accession,
                    profile_identifier=accession,
                    profile_locator=locator or source_url,
                    organism=str(item.get("taxon") or "").strip() or None,
                    cell_or_tissue_model=str(item.get("gdsType") or "").strip() or None,
                    dose=dose_match.group(0) if dose_match else None,
                    exposure_time=time_match.group(0) if time_match else None,
                    processing_level=("processed" if locator else None),
                    measurement_status="measured" if applied and locator else "unresolved",
                    compound_application_verified=applied,
                    raw_artifact_id=artifact_id,
                    raw_artifact_sha256=artifact_hash,
                    original_source_fields={
                        key: str(value)[:500]
                        for key, value in item.items()
                        if key
                        in {
                            "accession",
                            "gse",
                            "title",
                            "summary",
                            "taxon",
                            "ftpLink",
                            "suppfile",
                            "gdsType",
                        }
                        and value not in {None, ""}
                    },
                    evidence_references=[source_url],
                    exclusion_reason=exclusion,
                )
            )
        return rows

    def _normalized_records(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        content: bytes,
        content_type: str,
        *,
        source_url: str = EPA_TOXCAST_PUBLIC_RELEASE_PAGE,
    ) -> list[dict[str, Any]]:
        if (
            self.definition.adapter_id == "epa-comptox-toxcast"
            and operation == "check_epa_bioactivity_health"
        ):
            if content_type == TECHNICAL_HEALTH_STATUS_MIME:
                if content:
                    raise ValueError("EPA status-only health response must be empty")
            elif content_type == "text/plain":
                if not content.decode("utf-8", errors="strict").strip():
                    raise ValueError("EPA health response is empty")
            else:
                json.loads(content.decode("utf-8"))
            return []
        if (
            self.definition.adapter_id == "lincs-l1000"
            and operation == "check_lincs_geo_distribution_health"
        ):
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload.get("einforesult"), dict):
                raise ValueError("LINCS GEO distribution health response is malformed")
            return []
        if self.definition.adapter_id == "epa-toxcast-public-downloads":
            if content_type != "text/html":
                raise _ReviewedManifestParseError(
                    "epa_manifest_mime_mismatch",
                    "mime_validation",
                    "EPA public release manifest must be official HTML.",
                )
            parser = _BoundedManifestHTMLParser()
            try:
                parser.feed(content.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise _ReviewedManifestParseError(
                    "epa_manifest_html_parse_failed",
                    "html_parsing",
                    "EPA public release HTML could not be parsed safely.",
                ) from exc
            page_text = " ".join(" ".join([*parser.text_parts, *parser.metadata]).split())
            link_manifest_text = " ".join(f"{label} {href}" for href, label in parser.links)
            bounded_manifest_text = " ".join(f"{page_text} {link_manifest_text}".split())[:150_000]
            if re.search(r"\b(?:log\s*in|sign\s*in)\b", page_text, flags=re.I) and re.search(
                r"\b(?:password|username|account)\b", page_text, flags=re.I
            ):
                raise _ReviewedManifestParseError(
                    "epa_manifest_login_page",
                    "release_marker_validation",
                    "EPA public release operation returned a login page.",
                )
            if re.search(
                r"\b(?:page\s+not\s+found|access\s+denied|internal\s+server\s+error)\b",
                page_text,
                flags=re.I,
            ):
                raise _ReviewedManifestParseError(
                    "epa_manifest_error_page",
                    "release_marker_validation",
                    "EPA public release operation returned an error page.",
                )
            if not re.search(r"\bToxCast\b", bounded_manifest_text, flags=re.I):
                raise _ReviewedManifestParseError(
                    "epa_manifest_release_marker_missing",
                    "release_marker_validation",
                    "EPA public release page lacks the required ToxCast source-family marker.",
                )
            release_match = re.search(
                r"\binvitrodb(?:[\s_-]+database)?(?:[\s_-]+(?:release|version))?"
                r"[\s_-]*v?([0-9]+(?:\.[0-9]+)+)",
                bounded_manifest_text,
                flags=re.I,
            )
            if release_match is None:
                raise _ReviewedManifestParseError(
                    "epa_manifest_release_title_missing",
                    "release_version_parsing",
                    "EPA public release page lacks a bounded invitrodb release title and version.",
                )
            date_match = re.search(
                r"\b(?:release(?:d|\s+date)?|updated)\s*(?:on|:)?\s*"
                r"((?:19|20)[0-9]{2}-[01][0-9]-[0-3][0-9]|"
                r"(?:January|February|March|April|May|June|July|August|September|October|"
                r"November|December)(?:\s+[0-3]?[0-9],?)?\s+(?:19|20)[0-9]{2})",
                bounded_manifest_text,
                flags=re.I,
            )
            doi_match = re.search(
                r"\b10\.23645/epacomptox\.[0-9]+\.v([0-9]+)\b",
                bounded_manifest_text,
                flags=re.I,
            )
            if doi_match is None:
                raise _ReviewedManifestParseError(
                    "epa_manifest_citation_missing",
                    "official_link_extraction",
                    "EPA public release page lacks a bounded official citation DOI.",
                )
            citation_doi = doi_match.group(0).lower()
            citation_url = f"https://doi.org/{citation_doi}"
            availability_patterns = {
                "database package": (r"\b(?:database(?:[\s_-]+package)?|invitrodb[\s_-]+files?)\b"),
                "summary files": r"\bsummary[\s_-]+files?\b",
                "assay information": (
                    r"\bassay[\s_-]+(?:information|annotations?|target[\s_-]+mappings?)\b"
                ),
                "release notes": r"\brelease[\s_-]+notes?\b",
                "plots": r"\bplots?\b",
            }
            available_artifacts = [
                label
                for label, pattern in availability_patterns.items()
                if re.search(pattern, bounded_manifest_text, flags=re.I)
            ]
            if not available_artifacts or not re.search(
                r"\b(?:public|download|files?|data)\b", bounded_manifest_text, flags=re.I
            ):
                raise _ReviewedManifestParseError(
                    "epa_manifest_public_data_missing",
                    "official_link_extraction",
                    "EPA public release page lacks bounded public-data availability markers.",
                )
            official_links: list[tuple[str, str]] = []
            for href, label in parser.links:
                resolved = urljoin(source_url, href)
                parsed = urlparse(resolved)
                host = (parsed.hostname or "").casefold().rstrip(".")
                if (
                    parsed.scheme == "https"
                    and parsed.username is None
                    and parsed.password is None
                    and host in EPA_PUBLIC_DISTRIBUTION_HOSTS
                ):
                    combined = f"{label} {parsed.path}".casefold()
                    if any(
                        marker in combined
                        for marker in (
                            "assay",
                            "chemical",
                            "database",
                            "download",
                            "doi.org",
                            "file",
                            "figshare",
                            "invitrodb",
                            "note",
                            "plot",
                            "release",
                            "summary",
                            "toxcast",
                        )
                    ):
                        official_links.append(
                            (resolved[:2_000], label or parsed.path.rsplit("/", 1)[-1])
                        )
            deduplicated_links = list(dict.fromkeys(url for url, _label in official_links))[:25]
            release_version = release_match.group(1) if release_match else None
            release_date = date_match.group(1) if date_match else None
            return [
                {
                    "stable_identifier": (
                        f"invitrodb-v{release_version}"
                        if release_version
                        else "invitrodb-public-release"
                    ),
                    "source_release_version": release_version,
                    "source_release_date": release_date,
                    "manifest_verification_status": "public_manifest_verified",
                    "measurement_fields": [
                        "activity-call availability",
                        "continuous-measurement availability",
                    ],
                    "identifier_fields": [
                        "official chemical identity/archive metadata",
                        f"citation DOI {citation_doi}",
                    ],
                    "downloadable_artifacts": available_artifacts,
                    "official_evidence_references": [
                        source_url,
                        citation_url,
                        *deduplicated_links,
                    ],
                    "access_status": "requires_download",
                    "count_status": "not_computed",
                    "licence_access_status": "metadata_only",
                    "strengths": [
                        (
                            "The reviewed public Clowder release for the EPA ToxCast source "
                            "family was parsed as a bounded manifest."
                        )
                    ],
                    "limitations": [
                        "The full invitrodb database and activity tables were not downloaded.",
                        (
                            "Exact assay and compound counts were not computed from the "
                            "bounded manifest."
                        ),
                    ],
                    "unresolved_fields": [
                        field
                        for field, value in (
                            ("release version", release_version),
                            ("release date", release_date),
                            ("official citation DOI", citation_doi),
                        )
                        if not value
                    ]
                    + [
                        f"availability marker: {label}"
                        for label in availability_patterns
                        if label not in available_artifacts
                    ],
                    "next_required_ingestion_action": (
                        "Human review must authorize any separately bounded download and ingestion."
                    ),
                }
            ]
        if content_type in {"text/plain", "geo/text"} and self.definition.adapter_id in {
            "ncbi-geo-series",
            "lincs-l1000",
            "ncbi-supporting-metadata",
        }:
            text = content.decode("utf-8", errors="replace")
            accession = request.stable_identifier or self._geo_field(text, "Series_geo_accession")
            if accession is None and request.source_identifiers:
                accession = request.source_identifiers[0]
            if self.definition.adapter_id == "ncbi-supporting-metadata":
                return [
                    {
                        "stable_identifier": accession,
                        "title": self._geo_field(text, "Series_title"),
                        "downloadable_artifacts": ["official supplementary-file listing"],
                        "access_status": "metadata_only",
                        "count_status": "metadata_only",
                        "unresolved_fields": ["licence terms"],
                    }
                ]
            if self.definition.adapter_id == "lincs-l1000":
                return [
                    {
                        "stable_identifier": accession,
                        "title": self._geo_field(text, "Series_title"),
                        "modality": "chemical perturbation transcriptomics",
                        "identifier_fields": [
                            "perturbagen ID",
                            "compound name",
                            "available chemical identity fields",
                            "signature ID",
                        ],
                        "experimental_context_fields": [
                            "cell line",
                            "dose",
                            "exposure duration",
                            "processing level",
                            "feature space",
                        ],
                        "downloadable_artifacts": [
                            "bounded metadata manifest",
                            "processed signature manifest",
                            "matrix manifest",
                        ],
                        "access_status": "requires_download",
                        "count_status": "metadata_only",
                        "unresolved_fields": [
                            "bounded signature count",
                            "bounded compound count",
                            "release version",
                        ],
                    }
                ]
            return [
                {
                    "stable_identifier": accession,
                    "title": self._geo_field(text, "Series_title"),
                    "target": None,
                    "modality": "chemical perturbation transcriptomics",
                    "identifier_fields": ["compound name"],
                    "experimental_context_fields": [
                        "cell or tissue",
                        "dose",
                        "exposure time",
                        "control",
                    ],
                    "downloadable_artifacts": ["processed matrix", "raw matrix"],
                    "count_status": "metadata_only",
                    "access_status": "requires_download",
                }
            ]
        if (
            self.definition.adapter_id == "pubchem-bioassay"
            and operation == "extract_activity_result_rows"
        ):
            csv_reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="strict")))
            cids = [
                f"CID:{value}"
                for row in csv_reader
                if re.fullmatch(
                    r"[1-9][0-9]{0,11}",
                    value := str(row.get("PUBCHEM_CID") or "").strip(),
                )
            ]
            aid = str(request.stable_identifier or "").upper()
            modality = str(getattr(request, "modality_by_source", {}).get(aid) or "") or None
            return [
                {
                    "stable_identifier": aid,
                    "candidate_modalities": [modality] if modality else [],
                    "modality": modality,
                    "identifier_fields": ["PubChem AID", "PubChem CID"],
                    "sampled_compound_identifiers": list(dict.fromkeys(cids))[:100],
                    "measurement_fields": ["activity outcome", "activity value"],
                    "downloadable_artifacts": ["compound-level activity result table"],
                    "access_status": "verified_available",
                    "count_status": "exact",
                    "exact_counts": {"inspected_result_rows": len(cids)},
                    "validation_status": "verified",
                }
            ]
        payload = json.loads(content.decode("utf-8"))
        if self.definition.adapter_id == "epa-comptox-toxcast":
            if isinstance(payload, list):
                source_records = payload
            else:
                source_records = next(
                    (
                        payload.get(name)
                        for name in ("content", "results", "assayEndpoints", "records")
                        if isinstance(payload.get(name), list)
                    ),
                    [],
                )
            if operation == "inspect_epa_activity_availability":
                return [
                    {
                        "stable_identifier": f"AEID:{item.get('aeid')}",
                        "measurement_fields": [
                            "activeMc",
                            "totalMc",
                            "activeSc",
                            "totalSc",
                        ],
                        "exact_counts": {
                            key: item.get(key)
                            for key in ("activeMc", "totalMc", "activeSc", "totalSc")
                        },
                        "access_status": "verified_available",
                        "count_status": "exact",
                    }
                    for item in source_records
                    if isinstance(item, dict) and item.get("aeid") is not None
                ]
            if operation == "inspect_epa_compound_identifier_fields":
                return [
                    {
                        "stable_identifier": (
                            str(item.get("dtxsid") or item.get("spid") or item.get("chid"))
                            if any(item.get(key) is not None for key in ("dtxsid", "spid", "chid"))
                            else "unresolved"
                        ),
                        "source_assay_identifier": f"AEID:{item.get('aeid')}",
                        "identifier_fields": [
                            key.upper()
                            for key in ("dtxsid", "spid", "chid", "casn")
                            if item.get(key) is not None
                        ],
                        "measurement_fields": [
                            key
                            for key in ("hitc", "actp", "respMax", "modelType", "fitc")
                            if item.get(key) is not None
                        ],
                        "access_status": "verified_available",
                        "count_status": "record_level",
                    }
                    for item in source_records
                    if isinstance(item, dict)
                ]
            if operation == "search_epa_assays":
                query_terms = [
                    term.casefold()
                    for term in (
                        request.query,
                        request.biological_target,
                        request.endpoint_modality,
                    )
                    if term
                ]
                source_records = [
                    item
                    for item in source_records
                    if isinstance(item, dict)
                    and (
                        not query_terms
                        or all(
                            term in json.dumps(item, sort_keys=True).casefold()
                            for term in query_terms
                        )
                    )
                ]
            return [
                {
                    "stable_identifier": str(
                        item.get("aeid")
                        or item.get("assayEndpointId")
                        or item.get("endpointId")
                        or item.get("id")
                        or "unresolved"
                    ),
                    "title": item.get("assayComponentEndpointName") or item.get("assayName"),
                    "target": item.get("assayComponentTargetDesc")
                    or item.get("intendedTargetFamily"),
                    "modality": item.get("assayFunctionType"),
                    "measurement_fields": [
                        field
                        for field in (
                            item.get("normalizedDataType"),
                            item.get("parameterReadoutType"),
                        )
                        if field
                    ],
                    "identifier_fields": (["PubChem AID"] if item.get("aid") is not None else []),
                    "experimental_context_fields": [
                        field
                        for field in (
                            item.get("assayComponentName"),
                            item.get("assayDesignType"),
                            item.get("assayFormatType"),
                            item.get("cellShortName"),
                            item.get("timepointHr"),
                        )
                        if field
                    ],
                    "downloadable_artifacts": ([]),
                    "access_status": "metadata_only",
                    "count_status": "metadata_only",
                    "unresolved_fields": [
                        field
                        for field, value in (
                            ("assay source", item.get("assaySourceName")),
                            ("target annotation", item.get("assayComponentTargetDesc")),
                        )
                        if not value
                    ],
                }
                for item in source_records[: request.maximum_results]
                if isinstance(item, dict)
            ]
        if isinstance(payload.get("records"), list):
            return [item for item in payload["records"] if isinstance(item, dict)]
        if (
            self.definition.adapter_id in {"pubchem-bioassay", "ncbi-geo-series", "lincs-l1000"}
            and "esearchresult" in payload
        ):
            activity_search = self.definition.adapter_id == "pubchem-bioassay"
            prefix = "AID:" if activity_search else "GDS_UID:"
            return [
                {
                    "stable_identifier": f"{prefix}{item}",
                    "title": "",
                    "candidate_modalities": (
                        [str(request.endpoint_modality)]
                        if activity_search and request.endpoint_modality
                        else []
                    ),
                    "modality": (
                        "chemical perturbation transcriptomics"
                        if self.definition.adapter_id == "lincs-l1000"
                        else None
                    ),
                    "count_status": "metadata_only",
                    "validation_status": "metadata_candidate",
                    "unresolved_fields": (
                        [
                            "assay title",
                            "verified target",
                            "assay format",
                            "activity endpoint",
                            "compound-level record availability",
                        ]
                        if activity_search
                        else ["GSE accession", "study design", "matrix availability"]
                    ),
                }
                for item in payload.get("esearchresult", {}).get("idlist", [])
            ]
        if self.definition.adapter_id == "pubchem-bioassay" and isinstance(
            payload.get("IdentifierList"), dict
        ):
            identifiers = [
                f"CID:{item}"
                for item in payload["IdentifierList"].get("CID", [])[:10]
                if isinstance(item, int) or str(item).isdigit()
            ]
            return [
                {
                    "stable_identifier": request.stable_identifier,
                    "identifier_fields": ["PubChem CID"],
                    "sampled_compound_identifiers": identifiers,
                    "access_status": ("verified_available" if identifiers else "metadata_only"),
                    "count_status": "partial",
                    "validation_status": "partial",
                    "unresolved_fields": (
                        [] if identifiers else ["sampled active compound identifiers"]
                    ),
                }
            ]
        if self.definition.adapter_id == "pubchem-compound":
            if operation == "resolve_compound_synonyms":
                return [
                    {
                        "stable_identifier": f"CID:{item.get('CID')}",
                        "identifier_fields": ["PubChem CID", "verified synonyms"],
                        "sampled_compound_identifiers": [f"CID:{item.get('CID')}"],
                        "access_status": "verified_available",
                        "count_status": "partial",
                    }
                    for item in payload.get("InformationList", {}).get("Information", [])
                    if item.get("CID") is not None
                ]
            return [
                {
                    "stable_identifier": f"CID:{item.get('CID')}",
                    "title": item.get("Title", ""),
                    "identifier_fields": ["PubChem CID", "InChIKey"],
                    "structure_fields": ["canonical SMILES", "isomeric SMILES"],
                    "access_status": "verified_available",
                    "count_status": "partial",
                }
                for item in payload.get("PropertyTable", {}).get("Properties", [])
                if item.get("CID") is not None
            ]
        result = payload.get("result", {})
        records = []
        for uid in result.get("uids", []):
            item = result.get(str(uid), {})
            is_geo_summary = self.definition.adapter_id in {
                "ncbi-geo-series",
                "ncbi-supporting-metadata",
            } and (request.stable_identifier or "").upper().startswith("GDS_UID:")
            if is_geo_summary:
                accession = str(item.get("accession") or item.get("gse") or "").upper()
                stable_identifier = str(request.stable_identifier)
                summary = str(item.get("summary") or "")
                context_fields = [
                    value
                    for value in (
                        (
                            f"cell or tissue context: {item.get('taxon')}"
                            if item.get("taxon")
                            else None
                        ),
                        (
                            f"sample count: {item.get('n_samples')}"
                            if item.get("n_samples")
                            else None
                        ),
                        (
                            "dose/time metadata present"
                            if re.search(
                                r"\b(?:dose|hour|hours|day|days)\b",
                                summary,
                                flags=re.I,
                            )
                            else None
                        ),
                        "sample design summary present" if summary else None,
                    )
                    if value
                ]
                downloadables = [
                    label
                    for label, present in (
                        ("processed matrix", bool(item.get("suppfile") or item.get("ftpLink"))),
                        ("official GEO download locator", bool(item.get("ftpLink"))),
                    )
                    if present
                ]
                records.append(
                    {
                        "stable_identifier": stable_identifier,
                        "title": item.get("title", ""),
                        "organism": item.get("taxon"),
                        "modality": "chemical perturbation transcriptomics",
                        "perturbation_type": (
                            "chemical perturbation"
                            if re.search(
                                r"\b(?:compound|chemical|drug|treat)\w*\b",
                                summary,
                                flags=re.I,
                            )
                            else None
                        ),
                        "experimental_context_fields": context_fields,
                        "downloadable_artifacts": downloadables,
                        "identifier_fields": [
                            "compound name",
                            *(
                                [f"GEO accession {accession}"]
                                if re.fullmatch(r"GSE[1-9][0-9]{1,8}", accession)
                                else []
                            ),
                        ],
                        "access_status": "metadata_only",
                        "count_status": "metadata_only",
                        "validation_status": "partial",
                        "unresolved_fields": [
                            field
                            for field, present in (
                                ("chemical perturbation context", bool(summary)),
                                ("cell or tissue model", bool(item.get("taxon"))),
                                ("processed matrix availability", bool(downloadables)),
                            )
                            if not present
                        ],
                    }
                )
                continue
            records.append(
                {
                    "stable_identifier": request.stable_identifier or f"AID:{uid}",
                    "title": item.get("title", item.get("name", "")),
                    "target": item.get("targetname"),
                    "organism": item.get("organism"),
                    "modality": item.get("assaytype") or item.get("activityoutcome"),
                    "measurement_fields": [
                        value
                        for value in (
                            "activity outcome",
                            (
                                f"activity endpoint: {item.get('activityoutcome')}"
                                if item.get("activityoutcome")
                                else None
                            ),
                        )
                        if value
                    ],
                    "identifier_fields": ["PubChem AID", "PubChem CID"],
                    "experimental_context_fields": [
                        value
                        for value in (
                            f"assay format: {item.get('assaytype')}"
                            if item.get("assaytype")
                            else None,
                            f"organism: {item.get('organism')}" if item.get("organism") else None,
                        )
                        if value
                    ],
                    "downloadable_artifacts": ["compound-level activity result table"],
                    "access_status": "requires_download",
                    "count_status": "metadata_only",
                    "validation_status": "partial",
                }
            )
        if records:
            return records
        return [
            {
                "stable_identifier": request.stable_identifier or "linked-metadata",
                "title": "Official linked metadata",
                "access_status": "metadata_only",
                "count_status": "metadata_only",
            }
        ]

    @staticmethod
    def _geo_field(text: str, name: str) -> str | None:
        match = re.search(rf"^!{re.escape(name)}\s*=\s*(.+)$", text, flags=re.M)
        return match.group(1).strip() if match else None

    def _observation(
        self,
        operation: str,
        record: dict[str, Any],
        *,
        source_url: str,
        artifact_id: str,
        artifact_hash: str,
    ) -> VerifiedSourceObservation:
        stable = str(record.get("stable_identifier") or "unresolved")
        unsafe_identifier = not re.fullmatch(r"[A-Za-z0-9_.:-]{1,300}", stable)
        if unsafe_identifier:
            stable = "unresolved"
        digest = hashlib.sha256(
            canonical_json(
                {
                    "adapter": self.definition.adapter_id,
                    "version": self.definition.adapter_version,
                    "operation": operation,
                    "stable": stable,
                    "artifact": artifact_hash,
                }
            ).encode()
        ).hexdigest()
        roles = [
            ComponentRole(item)
            for item in record.get("source_roles", self.definition.supported_component_roles)
        ]
        access = CapabilityStatus(record.get("access_status", "metadata_only"))
        validation = SourceValidationStatus(
            record.get(
                "validation_status",
                "verified" if stable != "unresolved" else "unresolved",
            )
        )
        sanitization_warnings: list[str] = []

        def safe_text(value: object | None, field: str) -> str | None:
            if value is None:
                return None
            sanitized = sanitize_untrusted_text(str(value), source_id=f"{stable}:{field}")
            if sanitized["prompt_injection_warnings"]:
                sanitization_warnings.append(
                    f"Instruction-like content was removed from source field {field}."
                )
                return "[untrusted instruction-like source text omitted]"
            return str(sanitized["untrusted_text"])

        def safe_list(values: object, field: str) -> list[str]:
            if not isinstance(values, list):
                return []
            return [cleaned for value in values[:100] if (cleaned := safe_text(value, field))]

        raw_counts = record.get("exact_counts", {})
        if not isinstance(raw_counts, dict):
            raw_counts = {}
        references = [source_url]
        if self.definition.adapter_id == "epa-toxcast-public-downloads":
            for value in record.get("official_evidence_references", []):
                parsed = urlparse(str(value))
                host = (parsed.hostname or "").casefold().rstrip(".")
                if (
                    parsed.scheme == "https"
                    and parsed.username is None
                    and parsed.password is None
                    and host in EPA_PUBLIC_DISTRIBUTION_HOSTS
                ):
                    references.append(str(value)[:2_000])
        references = list(dict.fromkeys(references))[:100]
        return VerifiedSourceObservation(
            observation_id=deterministic_id("obs", self.definition.adapter_id, digest),
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            review_policy_version=self.definition.review_policy_version,
            source_system=self.definition.official_source_system,
            stable_source_identifier=stable,
            source_roles=roles,
            request_operation=operation,
            public_validation_status=validation,
            source_title=safe_text(record.get("title"), "title"),
            organism=safe_text(record.get("organism"), "organism"),
            target=safe_text(record.get("target"), "target"),
            modality=safe_text(record.get("modality"), "modality"),
            candidate_modalities=safe_list(
                record.get("candidate_modalities", []), "candidate_modality"
            ),
            perturbation_type=safe_text(record.get("perturbation_type"), "perturbation_type"),
            measurement_fields=safe_list(record.get("measurement_fields", []), "measurement"),
            identifier_fields=safe_list(record.get("identifier_fields", []), "identifier"),
            sampled_compound_identifiers=safe_list(
                record.get("sampled_compound_identifiers", []),
                "sampled_compound_identifier",
            ),
            structure_fields=safe_list(record.get("structure_fields", []), "structure"),
            experimental_context_fields=safe_list(
                record.get("experimental_context_fields", []), "experimental_context"
            ),
            data_access_status=access,
            downloadable_artifact_types=safe_list(
                record.get("downloadable_artifacts", []), "downloadable_artifact"
            ),
            exact_counts={
                str(key): value
                for key, value in raw_counts.items()
                if re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", str(key))
                and isinstance(value, int)
                and value >= 0
            },
            count_status=ObservationCountStatus(record.get("count_status", "not_computed")),
            source_release_version=safe_text(
                record.get("source_release_version"), "source_release_version"
            ),
            source_release_date=safe_text(record.get("source_release_date"), "source_release_date"),
            manifest_verification_status=record.get("manifest_verification_status"),
            licence_access_status=CapabilityStatus(
                record.get("licence_access_status", "metadata_only")
            ),
            official_evidence_references=references,
            retrieved_at=utc_text(),
            response_artifact_hash=artifact_hash,
            response_artifact_id=artifact_id,
            strengths=safe_list(
                record.get(
                    "strengths",
                    ["Official-source metadata was parsed deterministically."],
                ),
                "strength",
            ),
            limitations=[
                *safe_list(record.get("limitations", []), "limitation"),
                *sanitization_warnings,
                *(
                    ["Source identifier failed the stable-identifier policy."]
                    if unsafe_identifier
                    else []
                ),
            ],
            unresolved_fields=safe_list(record.get("unresolved_fields", []), "unresolved"),
            next_required_ingestion_action=safe_text(
                record.get(
                    "next_required_ingestion_action",
                    "Review the source inventory before any bounded ingestion is authorized.",
                ),
                "next_ingestion_action",
            )
            or "Review the source inventory before any bounded ingestion is authorized.",
        )


class ReviewedSourceAdapterRegistry:
    """Fail-closed registry: only approved definitions are exposed to live agents."""

    MANDATORY_ROLES = frozenset(
        {
            ComponentRole.ENDPOINT_ACTIVITY,
            ComponentRole.TRANSCRIPTOMIC_MATRIX,
            ComponentRole.COMPOUND_IDENTITY,
            ComponentRole.CHEMICAL_STRUCTURE,
            ComponentRole.PROVENANCE_LICENSE,
        }
    )
    MANDATORY_ADAPTER_IDS = frozenset(
        {
            "pubchem-bioassay",
            "epa-toxcast-public-downloads",
            "ncbi-geo-series",
            "lincs-l1000",
            "pubchem-compound",
            "ncbi-supporting-metadata",
        }
    )

    def __init__(self, adapters: Iterable[ReviewedSourceAdapter] = ()) -> None:
        self._adapters: dict[str, ReviewedSourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ReviewedSourceAdapter) -> None:
        definition = adapter.definition
        if definition.adapter_id in self._adapters:
            raise ValueError(f"reviewed adapter already registered: {definition.adapter_id}")
        self._adapters[definition.adapter_id] = adapter

    def approved(self) -> list[ReviewedSourceAdapter]:
        return [
            item
            for item in self._adapters.values()
            if item.definition.review_status is AdapterReviewStatus.APPROVED
            and item.definition.review_policy_version == REVIEW_POLICY_VERSION
        ]

    def readiness(self) -> dict[str, Any]:
        covered = {
            role
            for adapter in self.approved()
            for role in adapter.definition.supported_component_roles
        }
        missing = sorted(self.MANDATORY_ROLES - covered, key=lambda item: item.value)
        approved = self.approved()
        approved_by_id = {item.definition.adapter_id: item for item in approved}
        missing_adapters = sorted(self.MANDATORY_ADAPTER_IDS - approved_by_id.keys())
        authenticated_epa = approved_by_id.get("epa-comptox-toxcast")
        authenticated_epa_available = bool(
            authenticated_epa and authenticated_epa._epa_comptox_api_key
        )
        public_epa_ready = "epa-toxcast-public-downloads" in approved_by_id
        return {
            "schema_version": "1.1.0",
            "ready": not missing and not missing_adapters,
            "review_policy_version": REVIEW_POLICY_VERSION,
            "approved_adapter_count": len(approved),
            "approved_adapter_ids": sorted(item.definition.adapter_id for item in approved),
            "all_health_probe_capable": all(
                item.definition.health_probe_capable for item in approved
            ),
            "registry_fingerprint": hashlib.sha256(
                canonical_json(self.public_inventory()).encode()
            ).hexdigest(),
            "covered_roles": sorted(item.value for item in covered),
            "missing_roles": [item.value for item in missing],
            "missing_required_adapter_ids": missing_adapters,
            "epa_authenticated_api": {
                "adapter_id": "epa-comptox-toxcast",
                "status": (
                    "available" if authenticated_epa_available else "authenticated_api_unavailable"
                ),
                "required_for_discovery": False,
                "access_mode": "authenticated_api",
                "optional_acceleration_only": True,
            },
            "epa_public_data_releases": {
                "adapter_id": "epa-toxcast-public-downloads",
                "status": "ready" if public_epa_ready else "not_ready",
                "sufficient_for_first_controlled_discovery": public_epa_ready,
                "access_mode": "public_release",
                "source_of_record": True,
                "requires_api_key": False,
            },
            "lincs_public_releases": {
                "adapter_id": "lincs-l1000",
                "status": "ready" if "lincs-l1000" in approved_by_id else "not_ready",
                "requires_api_key": False,
            },
            "source_retries": max(
                (item.definition.source_retry_count for item in approved), default=0
            ),
        }

    def public_inventory(self) -> list[dict[str, Any]]:
        return [
            {
                **item.definition.model_dump(mode="json"),
                "operation_runtime_status": {
                    operation: item.operation_runtime_status(operation)
                    for operation in item.definition.approved_operations
                },
            }
            for item in self.approved()
        ]

    def operation_is_available(self, operation: str) -> bool:
        candidates = [
            item for item in self.approved() if operation in item.definition.approved_operations
        ]
        return len(candidates) == 1 and candidates[0].operation_runtime_status(operation) == "ready"

    def filter_available_operations(self, operations: Iterable[str]) -> list[str]:
        reviewed_operations = {
            operation
            for adapter in self.approved()
            for operation in adapter.definition.approved_operations
        }
        return [
            operation
            for operation in operations
            if operation not in reviewed_operations or self.operation_is_available(operation)
        ]

    def execute(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> VerifiedSourceObservationBatch:
        batchable = {
            "fetch_activity_source_metadata",
            "inspect_activity_identifier_fields",
            "extract_activity_result_rows",
            "fetch_transcriptomic_source_metadata",
            "inspect_supporting_metadata",
            "inspect_official_file_listing",
            "inspect_linked_publications",
            "inspect_source_access",
        }
        identifiers = list(request.source_identifiers)
        if operation not in batchable or not identifiers:
            return self._resolve(operation, request).execute(operation, request, invocation)

        batches: list[VerifiedSourceObservationBatch] = []
        source_errors: list[dict[str, str | bool]] = []
        failed_source_request_count = 0
        for index, identifier in enumerate(identifiers):
            single = request.model_copy(
                update={
                    "stable_identifier": identifier,
                    "source_identifiers": [],
                    "maximum_results": min(request.maximum_results, 10),
                }
            )
            single_invocation = invocation.model_copy(
                update={
                    "idempotency_key": (
                        f"{invocation.idempotency_key or operation}:item-{index + 1}"
                    )
                }
            )
            try:
                batches.append(
                    self._resolve(operation, single).execute(
                        operation,
                        single,
                        single_invocation,
                    )
                )
            except ReviewedSourceExecutionError as exc:
                started = bool(
                    exc.telemetry.http_status is not None
                    or exc.telemetry.final_allowlisted_host
                    or exc.telemetry.transport_attempts
                )
                if started:
                    failed_source_request_count += max(1, len(exc.telemetry.transport_attempts))
                source_errors.append(
                    {
                        "stable_identifier": identifier,
                        "error_code": exc.telemetry.failure_category or "source_request_failed",
                        "safe_message": str(exc)[:500],
                        "source_request_started": started,
                    }
                )
            except (ReviewedSourceInvocationError, ValueError) as exc:
                source_errors.append(
                    {
                        "stable_identifier": identifier,
                        "error_code": "source_input_or_request_invalid",
                        "safe_message": str(exc)[:500],
                        "source_request_started": False,
                    }
                )

        observations = list(
            {item.observation_id: item for batch in batches for item in batch.observations}.values()
        )
        artifact_ids = list(
            dict.fromkeys(
                artifact_id
                for batch in batches
                for artifact_id in batch.source_request_artifact_ids
            )
        )
        cache_statuses = {batch.cache_status for batch in batches}
        cache_status = (
            "fixture"
            if cache_statuses == {"fixture"}
            else "cached"
            if cache_statuses and cache_statuses <= {"cached"}
            else "live"
        )
        adapter = self._resolve(operation, request)
        return VerifiedSourceObservationBatch(
            adapter_id=adapter.definition.adapter_id,
            adapter_version=adapter.definition.adapter_version,
            operation=operation,
            cache_status=cast(Literal["live", "cached", "fixture"], cache_status),
            observations=observations,
            source_request_artifact_ids=artifact_ids,
            source_request_count=(
                sum(item.source_request_count for item in batches) + failed_source_request_count
            ),
            limitations=[
                *list(dict.fromkeys(value for batch in batches for value in batch.limitations)),
                *(
                    [f"{len(source_errors)} source item(s) failed safely; see source_errors."]
                    if source_errors
                    else []
                ),
            ],
            source_errors=source_errors,
            activity_rows=[row for batch in batches for row in batch.activity_rows],
            identity_bridge_rows=[row for batch in batches for row in batch.identity_bridge_rows],
            transcriptomic_profile_rows=[
                row for batch in batches for row in batch.transcriptomic_profile_rows
            ],
            inspected_record_count=sum(batch.inspected_record_count for batch in batches),
            excluded_record_count=sum(batch.excluded_record_count for batch in batches),
            transport_attempts=[
                attempt for batch in batches for attempt in batch.transport_attempts
            ][:20],
        )

    def execute_with_telemetry(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> ReviewedSourceExecutionResult:
        return self._resolve(operation, request).execute_with_telemetry(
            operation, request, invocation
        )

    def _resolve(
        self, operation: str, request: ReviewedSourceOperationInput
    ) -> ReviewedSourceAdapter:
        operation_candidates = [
            item for item in self.approved() if operation in item.definition.approved_operations
        ]
        candidates = [
            item
            for item in operation_candidates
            if request.source_system is None
            or request.source_system.casefold()
            in {
                item.definition.adapter_id.casefold(),
                item.definition.official_source_system.casefold(),
            }
        ]
        if len(candidates) != 1:
            expected = sorted({item.definition.adapter_id for item in operation_candidates})
            supplied = request.source_system or "not supplied"
            category = (
                "source_system_does_not_match_operation"
                if operation_candidates
                else "operation_not_approved"
            )
            raise ReviewedSourceInvocationError(
                (
                    f"Source system {supplied!r} is not approved for {operation}; "
                    f"expected one of: {', '.join(expected) or 'none'}."
                ),
                invocation_stage="adapter_resolution",
                validation_error_category=category,
                adapter_resolution_status="not_resolved",
                field_errors=[
                    {
                        "field": "source_system",
                        "category": category,
                        "message": (
                            f"Supplied {supplied!r}; approved adapter IDs are "
                            f"{', '.join(expected) or 'none'}."
                        )[:300],
                    }
                ],
            )
        return candidates[0]


def production_reviewed_source_registry(
    *,
    client: ScientificSourceClient,
    cache: SourceResponseCache,
    artifacts: LocalArtifactStore,
    epa_comptox_api_key: str | None = None,
) -> ReviewedSourceAdapterRegistry:
    return ReviewedSourceAdapterRegistry(
        ReviewedSourceAdapter(
            definition,
            client,
            cache,
            artifacts,
            epa_comptox_api_key=epa_comptox_api_key,
        )
        for definition in REVIEWED_ADAPTER_DEFINITIONS
    )


def adapter_registry_fingerprint(registry: ReviewedSourceAdapterRegistry) -> str:
    return hashlib.sha256(canonical_json(registry.public_inventory()).encode()).hexdigest()


def fixture_batch(
    definition: ReviewedSourceAdapterDefinition,
    operation: str,
    records: list[dict[str, Any]],
) -> VerifiedSourceObservationBatch:
    """Create source-neutral fixture evidence without transport or durable workflow state."""

    artifact_hash = hashlib.sha256(canonical_json(records).encode()).hexdigest()
    adapter = object.__new__(ReviewedSourceAdapter)
    adapter.definition = definition
    observations = [
        adapter._observation(
            operation,
            item,
            source_url=f"https://{definition.allowlisted_domains[0]}/",
            artifact_id=f"fixture-{artifact_hash[:24]}",
            artifact_hash=artifact_hash,
        )
        for item in records
    ]
    return VerifiedSourceObservationBatch(
        adapter_id=definition.adapter_id,
        adapter_version=definition.adapter_version,
        operation=operation,
        cache_status="fixture",
        observations=observations,
        source_request_artifact_ids=[],
        source_request_count=0,
    )
