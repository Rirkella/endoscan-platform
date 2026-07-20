"""Versioned, reviewed adapters for bounded official-source discovery.

The model selects an approved operation and typed arguments.  It never supplies a URL,
host, HTTP method, parser, or transport policy.  Network access is injected so the same
contracts are exercised by offline fixtures and the production client.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote, urlparse

from pydantic import Field, field_validator, model_validator

from .artifacts import LocalArtifactStore
from .contracts import SourceToolDiagnostic, StrictContract, ToolInvocation
from .repository import canonical_json, deterministic_id, utc_text
from .source_cache import SourceResponseCache
from .source_security import (
    ALLOWED_CONTENT_TYPES,
    ScientificSourceClient,
    sanitize_untrusted_text,
)
from .training_dataset import (
    CapabilityStatus,
    ComponentRole,
    ObservationCountStatus,
    SourceValidationStatus,
    VerifiedSourceObservation,
    VerifiedSourceObservationBatch,
)

REVIEW_POLICY_VERSION = "reviewed-source-adapters-v1"


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
    maximum_response_bytes: int = Field(ge=1_024, le=1_500_000)
    request_timeout_seconds: float = Field(gt=0, le=60)
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


class SourceHttpRequest(StrictContract):
    url: str
    params: dict[str, str | int]
    accepted_mime_types: list[str]
    allow_official_geo_text: bool = False
    required_credential: Literal["epa_comptox_api_key"] | None = None

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
        accepted_mime_types=sorted(ALLOWED_CONTENT_TYPES),
        maximum_response_bytes=500_000,
        request_timeout_seconds=15,
        maximum_redirects=2,
        source_retry_count=0,
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
        "summarize_activity_outcomes",
        "inspect_counter_screen_relationships",
    ],
    domains=["eutils.ncbi.nlm.nih.gov", "pubchem.ncbi.nlm.nih.gov"],
    parser="pubchem_bioassay_json_v1",
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
    ],
    domains=["pubchem.ncbi.nlm.nih.gov"],
    parser="pubchem_compound_properties_json_v1",
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
)

REVIEWED_ADAPTER_DEFINITIONS = (
    PUBCHEM_BIOASSAY_ADAPTER,
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
            stored = cached.http_metadata.get("execution_telemetry", {})
            telemetry = ReviewedSourceExecutionTelemetry.model_validate(
                {
                    "adapter_id": self.definition.adapter_id,
                    "adapter_version": self.definition.adapter_version,
                    "source_system": self.definition.official_source_system,
                    "operation_name": operation,
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
                    "retry_count": self.definition.source_retry_count,
                    "sanitized_diagnostic": stored.get("sanitized_diagnostic"),
                }
            )
            return ReviewedSourceExecutionResult(batch=batch, telemetry=telemetry)
        source_request = self._build_request(operation, request)
        request_host = (urlparse(source_request.url).hostname or "").lower().rstrip(".")
        if request_host not in self.definition.allowlisted_domains:
            raise ValueError("reviewed adapter request host is outside its definition allowlist")
        response = self.client.get(
            source_request.url,
            tool_name=operation,
            params=source_request.params,
            accepted_types=frozenset(source_request.accepted_mime_types),
            allow_official_geo_text=source_request.allow_official_geo_text,
            maximum_bytes=self.definition.maximum_response_bytes,
            headers=self._credential_headers(source_request),
        )
        artifact = self.artifacts.put_bytes(
            workflow_id=invocation.workflow_id,
            step_id=invocation.step_id,
            content=response.content,
            mime_type="application/octet-stream",
            artifact_type="immutable_source_response",
            logical_name=(
                f"source-response-{self.definition.adapter_id}-{operation}-{response.sha256[:16]}"
            ),
            producer=f"{self.definition.adapter_id}@{self.definition.adapter_version}",
            original_source=response.url,
            idempotency_key=invocation.idempotency_key or response.sha256,
        )
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
        except Exception as exc:
            diagnostic = (
                response.diagnostic.model_copy(
                    update={"parser_outcome": "failed", "source_artifact_id": artifact.id}
                )
                if response.diagnostic
                else None
            )
            telemetry = self._telemetry(
                operation=operation,
                cache_key=cache_key,
                response=response,
                artifact_id=artifact.id,
                artifact_sha256=artifact.sha256,
                parse_started=parse_started,
                total_started=total_started,
                parser_status="failed",
                cache_write_status="not_written",
                cache_status="miss_not_written",
                diagnostic=diagnostic,
            )
            raise ReviewedSourceExecutionError(
                "Reviewed source parser rejected the bounded response.", telemetry
            ) from exc
        batch = VerifiedSourceObservationBatch(
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            operation=operation,
            cache_status="live",
            observations=observations,
            source_request_artifact_ids=[artifact.id],
            source_request_count=1,
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
        response: Any,
        artifact_id: str,
        artifact_sha256: str,
        parse_started: float,
        total_started: float,
        parser_status: Literal["succeeded", "failed", "not_run"],
        cache_write_status: Literal["written", "not_written", "failed", "not_applicable"],
        cache_status: Literal["miss_written", "hit", "miss_not_written"],
        diagnostic: SourceToolDiagnostic | None,
    ) -> ReviewedSourceExecutionTelemetry:
        return ReviewedSourceExecutionTelemetry(
            adapter_id=self.definition.adapter_id,
            adapter_version=self.definition.adapter_version,
            source_system=self.definition.official_source_system,
            operation_name=operation,
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
            retry_count=self.definition.source_retry_count,
            sanitized_diagnostic=diagnostic,
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
                        "text/plain",
                    ],
                )
            search_term = query or request.stable_identifier
            if not search_term:
                raise ValueError("EPA assay operations require a typed query or stable identifier")
            if operation == "search_epa_assays":
                return SourceHttpRequest(
                    url=(
                        "https://comptox.epa.gov/ctx-api/bioactivity/search/contain/"
                        f"{quote(search_term, safe='')}"
                    ),
                    params={"top": request.maximum_results},
                    accepted_mime_types=["application/json"],
                    required_credential="epa_comptox_api_key",
                )
            stable_identifier = self._required_identifier(request)
            aeid = stable_identifier.removeprefix("AEID:").removeprefix("aeid:")
            if not re.fullmatch(r"[1-9][0-9]{0,9}", aeid):
                raise ValueError("EPA metadata operations require a numeric AEID")
            return SourceHttpRequest(
                url=(
                    "https://comptox.epa.gov/ctx-api/bioactivity/assay/search/by-aeid/"
                    f"{aeid}"
                ),
                params={"projection": "assay-all"},
                accepted_mime_types=["application/json"],
                required_credential="epa_comptox_api_key",
            )
        if self.definition.adapter_id == "lincs-l1000":
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
            if not re.fullmatch(r"GSE[1-9][0-9]{1,8}", stable):
                raise ValueError("LINCS metadata operations require a validated GSE accession")
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
            return SourceHttpRequest(
                url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "pcassay", "id": stable.removeprefix("AID:"), "retmode": "json"},
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
            namespace = (request.identifier_type or "cid").casefold()
            namespace = {
                "pubchem cid": "cid",
                "cid": "cid",
                "inchikey": "inchikey",
                "compound name": "name",
                "name": "name",
            }.get(namespace)
            if namespace is None:
                raise ValueError("compound identifier type is not approved")
            joined = ",".join(quote(item, safe="") for item in identifiers)
            return SourceHttpRequest(
                url=(
                    f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{namespace}/{joined}/"
                    "property/Title,CanonicalSMILES,IsomericSMILES,InChIKey/JSON"
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
                "id": ",".join(identifiers),
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
        records = self._normalized_records(operation, request, content, content_type)
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

    def _normalized_records(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        content: bytes,
        content_type: str,
    ) -> list[dict[str, Any]]:
        if (
            self.definition.adapter_id == "epa-comptox-toxcast"
            and operation == "check_epa_bioactivity_health"
        ):
            if content_type == "text/plain":
                if not content.decode("utf-8", errors="strict").strip():
                    raise ValueError("EPA health response is empty")
            else:
                json.loads(content.decode("utf-8"))
            return []
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
            return [
                {
                    "stable_identifier": str(
                        item.get("aeid")
                        or item.get("assayEndpointId")
                        or item.get("endpointId")
                        or item.get("id")
                        or "unresolved"
                    ),
                    "title": item.get("assayEndpointName")
                    or item.get("searchValue")
                    or item.get("searchValueDesc")
                    or item.get("endpointName")
                    or item.get("name"),
                    "target": item.get("biologicalTarget") or item.get("target"),
                    "modality": item.get("assayModality") or item.get("modality"),
                    "measurement_fields": [
                        field
                        for field in (
                            "activity call" if item.get("activityCallAvailable") else None,
                            (
                                "continuous measurement"
                                if item.get("continuousMeasurementAvailable")
                                else None
                            ),
                        )
                        if field
                    ],
                    "identifier_fields": [
                        field
                        for field in ("DTXSID", "CASRN", "PubChem CID")
                        if field in item.get("compoundIdentifierFields", [])
                    ],
                    "experimental_context_fields": [
                        field
                        for field in (
                            item.get("assayComponent"),
                            item.get("assayComponentName"),
                        )
                        if field
                    ],
                    "downloadable_artifacts": (
                        ["official downloadable artifact manifest"]
                        if item.get("downloadManifestAvailable")
                        else []
                    ),
                    "access_status": "metadata_only",
                    "count_status": "metadata_only",
                    "unresolved_fields": [
                        field
                        for field, value in (
                            ("release version", item.get("releaseVersion")),
                            ("related assay components", item.get("relatedAssayComponents")),
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
                    "modality": (
                        "chemical perturbation transcriptomics"
                        if self.definition.adapter_id == "lincs-l1000"
                        else None
                    ),
                    "count_status": "metadata_only",
                    "validation_status": "verified" if activity_search else "unresolved",
                    "unresolved_fields": [] if activity_search else ["GSE accession"],
                }
                for item in payload.get("esearchresult", {}).get("idlist", [])
            ]
        if self.definition.adapter_id == "pubchem-compound":
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
            records.append(
                {
                    "stable_identifier": request.stable_identifier or f"AID:{uid}",
                    "title": item.get("title", item.get("name", "")),
                    "target": item.get("targetname"),
                    "modality": item.get("activityoutcome"),
                    "measurement_fields": ["activity outcome"],
                    "identifier_fields": ["PubChem AID", "PubChem CID"],
                    "downloadable_artifacts": ["compound-level activity result table"],
                    "access_status": "requires_download",
                    "count_status": "metadata_only",
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
        reference = source_url
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
            target=safe_text(record.get("target"), "target"),
            modality=safe_text(record.get("modality"), "modality"),
            perturbation_type=safe_text(record.get("perturbation_type"), "perturbation_type"),
            measurement_fields=safe_list(record.get("measurement_fields", []), "measurement"),
            identifier_fields=safe_list(record.get("identifier_fields", []), "identifier"),
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
            licence_access_status=CapabilityStatus(
                record.get("licence_access_status", "metadata_only")
            ),
            official_evidence_references=[reference],
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
        return {
            "schema_version": "1.0.0",
            "ready": not missing,
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
            "source_retries": 0,
        }

    def public_inventory(self) -> list[dict[str, Any]]:
        return [item.definition.model_dump(mode="json") for item in self.approved()]

    def execute(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> VerifiedSourceObservationBatch:
        candidates = [
            item
            for item in self.approved()
            if operation in item.definition.approved_operations
            and (
                request.source_system is None
                or request.source_system.casefold()
                in {
                    item.definition.adapter_id.casefold(),
                    item.definition.official_source_system.casefold(),
                }
            )
        ]
        if len(candidates) != 1:
            raise ValueError("operation does not resolve to exactly one approved source adapter")
        return candidates[0].execute(operation, request, invocation)

    def execute_with_telemetry(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        invocation: ToolInvocation,
    ) -> ReviewedSourceExecutionResult:
        candidates = [
            item
            for item in self.approved()
            if operation in item.definition.approved_operations
            and (
                request.source_system is None
                or request.source_system.casefold()
                in {
                    item.definition.adapter_id.casefold(),
                    item.definition.official_source_system.casefold(),
                }
            )
        ]
        if len(candidates) != 1:
            raise ValueError("operation does not resolve to exactly one approved source adapter")
        return candidates[0].execute_with_telemetry(operation, request, invocation)


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
