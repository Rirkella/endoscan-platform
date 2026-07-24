"""Provider-neutral, resumable retrieval for paginated and multi-file catalogues.

The executor deliberately reuses the durable source-response cache, content-addressed
artifact store, and semantics-v2 DiscoveryExecutionLedger.  A successful HTTP response
is not a completion condition: every mandatory manifest item must be reconciled and its
checksum (when declared) must be verified before a task becomes ``completed``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field, field_validator, model_validator

from .artifacts import LocalArtifactStore
from .discovery_strategy import (
    ArtifactReference,
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    ImmutableV2Contract,
    deterministic_fingerprint,
)
from .source_cache import SourceResponseCache
from .source_governor import current_source_request_context
from .source_security import ScientificResponse, SourcePolicyError, SourceToolError


class ProviderItemKind(StrEnum):
    PAGE = "page"
    FILE = "file"


class ProviderResourceStatus(StrEnum):
    COMPLETED = "completed"
    CACHE_HIT = "cache_hit"
    FAILED = "failed"


class ProviderResourceRequest(ImmutableV2Contract):
    resource_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    logical_role: str = Field(min_length=2, max_length=120)
    operation_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    locator: str = Field(pattern=r"^https://[^\s]+$")
    item_kind: ProviderItemKind
    required: bool = True
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    accepted_mime_types: list[str] = Field(min_length=1, max_length=10)
    maximum_response_bytes: int = Field(ge=1_024, le=500_000_000)
    approved_request_hosts: list[str] = Field(default_factory=list, max_length=20)
    required_credential: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{2,79}$")
    cursor_or_file_token: str | None = Field(default=None, max_length=500)
    compression: str | None = Field(default=None, max_length=40)

    @field_validator("approved_request_hosts")
    @classmethod
    def approved_hosts_are_exact_hostnames(cls, values: list[str]) -> list[str]:
        for value in values:
            if (
                not value
                or value != value.lower().rstrip(".")
                or "://" in value
                or "/" in value
                or "@" in value
                or ":" in value
            ):
                raise ValueError("provider request allowlists require exact lowercase hostnames")
        return values


class ProviderRetrievalTask(ImmutableV2Contract):
    workflow_id: str = Field(min_length=3, max_length=160)
    task_id: str = Field(min_length=3, max_length=160)
    provider: str = Field(min_length=2, max_length=120)
    operation: str = Field(min_length=2, max_length=160)
    source_version: str = Field(min_length=1, max_length=160)
    source_locator: str = Field(pattern=r"^https://[^\s]+$")
    resources: list[ProviderResourceRequest] = Field(min_length=1, max_length=100)
    licence_and_provenance: list[str] = Field(min_length=1, max_length=50)
    timeout_seconds: float = Field(gt=0, le=180)
    maximum_transport_retries: int = Field(default=1, ge=0, le=1)
    idempotency_key: str = Field(min_length=3, max_length=160)
    task_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_task(self) -> ProviderRetrievalTask:
        resource_ids = [item.resource_id for item in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("provider resource IDs must be unique")
        payload = self.model_dump(mode="json", exclude={"task_fingerprint"})
        if deterministic_fingerprint(payload) != self.task_fingerprint:
            raise ValueError("provider retrieval task fingerprint does not match its content")
        return self

    @classmethod
    def create(cls, **values: Any) -> ProviderRetrievalTask:
        values["task_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


class ProviderTransportAttempt(ImmutableV2Contract):
    resource_id: str
    attempt_number: int = Field(ge=1, le=2)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    outcome: str = Field(min_length=2, max_length=80)
    request_left_process: bool
    exception_class: str | None = Field(default=None, max_length=160)
    safe_message: str | None = Field(default=None, max_length=500)
    http_status: int | None = Field(default=None, ge=100, le=599)
    source_error_category: str | None = Field(default=None, max_length=120)
    final_approved_host: str | None = Field(default=None, max_length=253)


class ProviderPageOrFileResult(ImmutableV2Contract):
    resource_id: str
    logical_role: str
    locator: str
    item_kind: ProviderItemKind
    status: ProviderResourceStatus
    source_version: str
    final_locator: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = Field(default=None, max_length=160)
    raw_artifact: ArtifactReference | None = None
    observed_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checksum_verified: bool
    response_bytes: int = Field(ge=0)
    parsed_record_count: int = Field(ge=0)
    retrieved_at: datetime | None = None
    cache_key: str | None = Field(default=None, max_length=64)
    transport_attempts: list[ProviderTransportAttempt] = Field(default_factory=list, max_length=2)
    error_classification: str | None = Field(default=None, max_length=160)
    safe_message: str | None = Field(default=None, max_length=500)


class ProviderCompletionProof(ImmutableV2Contract):
    task_id: str
    task_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    declared_resource_ids: list[str]
    mandatory_resource_ids: list[str]
    completed_resource_ids: list[str]
    missing_mandatory_resource_ids: list[str]
    checksum_verified_resource_ids: list[str]
    manifest_reconciled: bool
    all_mandatory_resources_complete: bool
    completed: bool
    completion_reason: str = Field(min_length=3, max_length=1000)


class ProviderCacheEntry(ImmutableV2Contract):
    resource_id: str
    cache_key: str = Field(min_length=64, max_length=64)
    raw_artifact: ArtifactReference
    source_locator: str
    source_version: str
    retrieved_at: datetime


class ProviderCacheManifest(ImmutableV2Contract):
    task_id: str
    task_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version: str
    source_locator: str
    licence_and_provenance: list[str]
    entries: list[ProviderCacheEntry] = Field(default_factory=list, max_length=100)


class ProviderExecutionOutcome(ImmutableV2Contract):
    task: ProviderRetrievalTask
    ledger_record: DiscoveryExecutionRecord
    page_or_file_results: list[ProviderPageOrFileResult]
    completion_proof: ProviderCompletionProof
    cache_manifest: ProviderCacheManifest
    normalized_resources: dict[str, dict[str, Any]]
    logical_tool_call_count: int = Field(ge=1)
    scientific_source_request_count: int = Field(ge=0)
    transport_attempt_count: int = Field(ge=0)


class ProviderTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        tool_name: str,
        accepted_types: frozenset[str],
        maximum_bytes: int,
        maximum_attempts: int,
        approved_request_hosts: frozenset[str] | None = None,
        headers: dict[str, str] | None = None,
        allow_official_geo_text: bool = False,
    ) -> ScientificResponse: ...


ProviderResourceParser = Callable[[ProviderResourceRequest, bytes], dict[str, Any]]


def _artifact_reference(descriptor: Any, artifact_type: str) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=descriptor.id,
        sha256=descriptor.sha256,
        artifact_type=artifact_type,
    )


def _safe_transport_message(exc: Exception) -> str:
    if isinstance(exc, SourceToolError):
        return str(exc)[:500]
    return f"Reviewed provider transport failed with {type(exc).__name__}."


class ProviderAuthenticationRequiredError(SourcePolicyError):
    """A reviewed provider credential is absent at the approved config boundary."""

    error_code = "PROVIDER_AUTHENTICATION_REQUIRED"
    retryable = False


class ProviderCredentialBoundary:
    """Resolve named credential bindings without serializing secret values."""

    def __init__(self, *, epa_comptox_api_key: str | None = None) -> None:
        self._epa_comptox_api_key = epa_comptox_api_key

    def headers_for(self, binding: str | None) -> dict[str, str] | None:
        if binding is None:
            return None
        if binding != "epa_comptox_api_key":
            raise SourcePolicyError("Provider resource uses an unsupported credential binding.")
        if not self._epa_comptox_api_key:
            raise ProviderAuthenticationRequiredError(
                "EPA CTX Bioactivity authentication is required for this reviewed operation."
            )
        return {"x-api-key": self._epa_comptox_api_key}

    def available(self, binding: str) -> bool:
        """Report configured credential availability without exposing its value."""

        if binding != "epa_comptox_api_key":
            return False
        return bool(self._epa_comptox_api_key)


def _error_classification(exc: Exception | None) -> str:
    if exc is None:
        return "transport_failure"
    if code := getattr(exc, "error_code", None):
        return str(code)
    if "timeout" in type(exc).__name__.lower():
        return "timeout"
    return "transport_failure"


class ProviderTaskExecutor:
    """Execute one immutable provider task with cache-aware file/page resumption."""

    def __init__(
        self,
        *,
        transport: ProviderTransport,
        cache: SourceResponseCache,
        artifacts: LocalArtifactStore,
        credential_boundary: ProviderCredentialBoundary | None = None,
    ) -> None:
        self.transport = transport
        self.cache = cache
        self.artifacts = artifacts
        self.credential_boundary = credential_boundary or ProviderCredentialBoundary()

    def execute(
        self,
        task: ProviderRetrievalTask,
        ledger_record: DiscoveryExecutionRecord,
        *,
        parser: ProviderResourceParser,
    ) -> ProviderExecutionOutcome:
        if ledger_record.task_id != task.task_id or ledger_record.provider != task.provider:
            raise ValueError("provider task does not match its discovery ledger record")

        results: list[ProviderPageOrFileResult] = []
        normalized: dict[str, dict[str, Any]] = {}
        source_request_count = 0
        transport_attempt_count = 0
        cache_entries: list[ProviderCacheEntry] = []

        for resource in task.resources:
            request_context = current_source_request_context()
            if request_context is not None and request_context.cancellation.cancelled:
                break
            arguments = {
                "task_fingerprint": task.task_fingerprint,
                "resource_id": resource.resource_id,
                "locator": resource.locator,
                "expected_sha256": resource.expected_sha256,
                "required_credential": resource.required_credential,
            }
            tool_name = re.sub(
                r"[^a-z0-9_]",
                "_",
                f"provider_retrieval_{task.provider}_{task.operation}".lower(),
            )[:79]
            cached = self.cache.get(tool_name, arguments, source_version=task.source_version)
            if cached is not None:
                parsed = dict(cached.parsed_output)
                normalized[resource.resource_id] = parsed
                artifact = ArtifactReference(
                    artifact_id=cached.raw_artifact_id,
                    sha256=cached.content_hash,
                    artifact_type="immutable_provider_source_response",
                )
                cache_entries.append(
                    ProviderCacheEntry(
                        resource_id=resource.resource_id,
                        cache_key=cached.cache_key,
                        raw_artifact=artifact,
                        source_locator=cached.source_url,
                        source_version=task.source_version,
                        retrieved_at=cached.retrieved_at,
                    )
                )
                results.append(
                    ProviderPageOrFileResult(
                        resource_id=resource.resource_id,
                        logical_role=resource.logical_role,
                        locator=resource.locator,
                        item_kind=resource.item_kind,
                        status=ProviderResourceStatus.CACHE_HIT,
                        source_version=task.source_version,
                        final_locator=str(
                            cached.http_metadata.get("final_locator") or cached.source_url
                        ),
                        http_status=int(cached.http_metadata.get("status_code", 200)),
                        content_type=str(cached.http_metadata.get("content_type") or "") or None,
                        raw_artifact=artifact,
                        observed_sha256=cached.content_hash,
                        expected_sha256=resource.expected_sha256,
                        checksum_verified=(
                            resource.expected_sha256 is None
                            or resource.expected_sha256 == cached.content_hash
                        ),
                        response_bytes=int(cached.http_metadata.get("response_bytes", 0)),
                        parsed_record_count=int(parsed.get("record_count", 0)),
                        retrieved_at=cached.retrieved_at,
                        cache_key=cached.cache_key,
                    )
                )
                continue

            attempts: list[ProviderTransportAttempt] = []
            response: ScientificResponse | None = None
            failure: Exception | None = None
            request_left_process = False
            for attempt_index in range(task.maximum_transport_retries + 1):
                started_at = datetime.now(UTC)
                started = time.monotonic()
                try:
                    headers = self.credential_boundary.headers_for(resource.required_credential)
                    transport_arguments: dict[str, Any] = {
                        "tool_name": tool_name,
                        "accepted_types": frozenset(resource.accepted_mime_types),
                        "maximum_bytes": resource.maximum_response_bytes,
                        "maximum_attempts": 1,
                        "approved_request_hosts": (
                            frozenset(resource.approved_request_hosts)
                            if resource.approved_request_hosts
                            else None
                        ),
                    }
                    if headers is not None:
                        transport_arguments["headers"] = headers
                    if resource.operation_id == "geo_series_soft":
                        transport_arguments["allow_official_geo_text"] = True
                    response = self.transport.get(
                        resource.locator,
                        **transport_arguments,
                    )
                except Exception as exc:  # normalized below; never expose source bodies
                    failure = exc
                    diagnostic = getattr(exc, "diagnostic", None)
                    retryable = bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, TimeoutError | ConnectionError | OSError
                    )
                    attempts.append(
                        ProviderTransportAttempt(
                            resource_id=resource.resource_id,
                            attempt_number=attempt_index + 1,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                            outcome=(
                                "timeout" if "timeout" in type(exc).__name__.lower() else "failed"
                            ),
                            request_left_process=not isinstance(
                                exc, ValueError | SourcePolicyError
                            ),
                            exception_class=type(exc).__name__,
                            safe_message=_safe_transport_message(exc),
                            http_status=getattr(diagnostic, "http_status", None),
                            source_error_category=getattr(
                                diagnostic, "source_error_category", None
                            ),
                            final_approved_host=getattr(diagnostic, "final_approved_host", None),
                        )
                    )
                    left_process = not isinstance(exc, ValueError | SourcePolicyError)
                    request_left_process = request_left_process or left_process
                    transport_attempt_count += int(left_process)
                    if not retryable or attempt_index >= task.maximum_transport_retries:
                        break
                    continue
                attempts.append(
                    ProviderTransportAttempt(
                        resource_id=resource.resource_id,
                        attempt_number=attempt_index + 1,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        outcome="completed",
                        request_left_process=True,
                    )
                )
                request_left_process = True
                transport_attempt_count += 1
                failure = None
                break
            source_request_count += int(request_left_process)

            if response is None:
                results.append(
                    ProviderPageOrFileResult(
                        resource_id=resource.resource_id,
                        logical_role=resource.logical_role,
                        locator=resource.locator,
                        item_kind=resource.item_kind,
                        status=ProviderResourceStatus.FAILED,
                        source_version=task.source_version,
                        checksum_verified=False,
                        response_bytes=0,
                        parsed_record_count=0,
                        transport_attempts=attempts,
                        error_classification=(_error_classification(failure)),
                        safe_message=(
                            _safe_transport_message(failure)
                            if failure
                            else "Provider transport failed."
                        ),
                    )
                )
                request_context = current_source_request_context()
                if request_context is not None and request_context.cancellation.cancelled:
                    break
                continue

            observed_sha256 = hashlib.sha256(response.content).hexdigest()
            descriptor = self.artifacts.put_bytes(
                workflow_id=task.workflow_id,
                content=response.content,
                mime_type="application/octet-stream",
                artifact_type="immutable_provider_source_response",
                logical_name=(
                    f"provider-{task.provider}-{task.task_fingerprint[:12]}-"
                    f"{resource.resource_id}-{observed_sha256[:12]}"
                ),
                producer=f"provider-executor:{task.provider}",
                original_source=resource.locator,
                idempotency_key=f"{task.idempotency_key}:{resource.resource_id}:raw",
            )
            raw_artifact = _artifact_reference(descriptor, "immutable_provider_source_response")
            checksum_verified = (
                resource.expected_sha256 is None or resource.expected_sha256 == observed_sha256
            )
            if not checksum_verified:
                results.append(
                    ProviderPageOrFileResult(
                        resource_id=resource.resource_id,
                        logical_role=resource.logical_role,
                        locator=resource.locator,
                        item_kind=resource.item_kind,
                        status=ProviderResourceStatus.FAILED,
                        source_version=task.source_version,
                        final_locator=response.url,
                        http_status=response.status_code,
                        content_type=response.content_type,
                        raw_artifact=raw_artifact,
                        observed_sha256=observed_sha256,
                        expected_sha256=resource.expected_sha256,
                        checksum_verified=False,
                        response_bytes=len(response.content),
                        parsed_record_count=0,
                        retrieved_at=datetime.fromtimestamp(response.retrieved_at, UTC),
                        transport_attempts=attempts,
                        error_classification="checksum_mismatch",
                        safe_message="Provider resource failed its reviewed checksum.",
                    )
                )
                continue

            try:
                parsed = parser(resource, response.content)
            except Exception as exc:
                results.append(
                    ProviderPageOrFileResult(
                        resource_id=resource.resource_id,
                        logical_role=resource.logical_role,
                        locator=resource.locator,
                        item_kind=resource.item_kind,
                        status=ProviderResourceStatus.FAILED,
                        source_version=task.source_version,
                        final_locator=response.url,
                        http_status=response.status_code,
                        content_type=response.content_type,
                        raw_artifact=raw_artifact,
                        observed_sha256=observed_sha256,
                        expected_sha256=resource.expected_sha256,
                        checksum_verified=True,
                        response_bytes=len(response.content),
                        parsed_record_count=0,
                        retrieved_at=datetime.fromtimestamp(response.retrieved_at, UTC),
                        transport_attempts=attempts,
                        error_classification="parser_failure",
                        safe_message=(f"Provider resource parser failed: {type(exc).__name__}."),
                    )
                )
                continue
            parsed_record_count = int(parsed.get("record_count", 0))
            normalized[resource.resource_id] = parsed
            cached = self.cache.put(
                tool_name,
                arguments,
                source_url=resource.locator,
                content_hash=observed_sha256,
                parsed_output=parsed,
                raw_artifact_id=descriptor.id,
                http_metadata={
                    "status_code": response.status_code,
                    "content_type": response.content_type,
                    "final_locator": response.url,
                    "response_bytes": len(response.content),
                    "transport_attempt_count": len(attempts),
                },
                source_version=task.source_version,
            )
            cache_entries.append(
                ProviderCacheEntry(
                    resource_id=resource.resource_id,
                    cache_key=cached.cache_key,
                    raw_artifact=raw_artifact,
                    source_locator=resource.locator,
                    source_version=task.source_version,
                    retrieved_at=cached.retrieved_at,
                )
            )
            results.append(
                ProviderPageOrFileResult(
                    resource_id=resource.resource_id,
                    logical_role=resource.logical_role,
                    locator=resource.locator,
                    item_kind=resource.item_kind,
                    status=ProviderResourceStatus.COMPLETED,
                    source_version=task.source_version,
                    final_locator=response.url,
                    http_status=response.status_code,
                    content_type=response.content_type,
                    raw_artifact=raw_artifact,
                    observed_sha256=observed_sha256,
                    expected_sha256=resource.expected_sha256,
                    checksum_verified=True,
                    response_bytes=len(response.content),
                    parsed_record_count=parsed_record_count,
                    retrieved_at=cached.retrieved_at,
                    cache_key=cached.cache_key,
                    transport_attempts=attempts,
                )
            )

        declared = [item.resource_id for item in task.resources]
        mandatory = [item.resource_id for item in task.resources if item.required]
        completed = [
            item.resource_id
            for item in results
            if item.status in {ProviderResourceStatus.COMPLETED, ProviderResourceStatus.CACHE_HIT}
            and item.checksum_verified
        ]
        missing = sorted(set(mandatory) - set(completed))
        proof = ProviderCompletionProof(
            task_id=task.task_id,
            task_fingerprint=task.task_fingerprint,
            declared_resource_ids=declared,
            mandatory_resource_ids=mandatory,
            completed_resource_ids=completed,
            missing_mandatory_resource_ids=missing,
            checksum_verified_resource_ids=[
                item.resource_id for item in results if item.checksum_verified
            ],
            manifest_reconciled=set(declared) == {item.resource_id for item in results},
            all_mandatory_resources_complete=not missing,
            completed=not missing and set(declared) == {item.resource_id for item in results},
            completion_reason=(
                "All mandatory manifest resources were retrieved or replayed and reconciled."
                if not missing
                else "Partial provider retrieval; mandatory resources remain: " + ", ".join(missing)
            ),
        )
        cache_manifest = ProviderCacheManifest(
            task_id=task.task_id,
            task_fingerprint=task.task_fingerprint,
            source_version=task.source_version,
            source_locator=task.source_locator,
            licence_and_provenance=task.licence_and_provenance,
            entries=cache_entries,
        )
        task_descriptor = self.artifacts.put_json(
            workflow_id=task.workflow_id,
            value=task.model_dump(mode="json"),
            artifact_type="provider_retrieval_task",
            logical_name=f"provider-task-{task.task_fingerprint}.json",
            producer="deterministic-provider-executor",
            idempotency_key=f"{task.idempotency_key}:retrieval-task",
        )
        proof_descriptor = self.artifacts.put_json(
            workflow_id=task.workflow_id,
            value=proof.model_dump(mode="json"),
            artifact_type="provider_completion_proof",
            logical_name=f"provider-completion-{task.task_fingerprint}.json",
            producer="deterministic-provider-executor",
            idempotency_key=f"{task.idempotency_key}:completion-proof",
        )
        manifest_descriptor = self.artifacts.put_json(
            workflow_id=task.workflow_id,
            value=cache_manifest.model_dump(mode="json"),
            artifact_type="provider_cache_manifest",
            logical_name=f"provider-cache-{task.task_fingerprint}.json",
            producer="deterministic-provider-executor",
            idempotency_key=f"{task.idempotency_key}:cache-manifest",
        )
        artifacts = {
            item.artifact_id: item
            for item in (
                *(item.raw_artifact for item in results if item.raw_artifact is not None),
                _artifact_reference(task_descriptor, "provider_retrieval_task"),
                _artifact_reference(proof_descriptor, "provider_completion_proof"),
                _artifact_reference(manifest_descriptor, "provider_cache_manifest"),
            )
        }
        updated_record = ledger_record.model_copy(
            update={
                "status": (
                    DiscoveryTaskStatus.COMPLETED
                    if proof.completed
                    else DiscoveryTaskStatus.RUNNING
                ),
                "pages_or_cursors_attempted": sorted(
                    set(ledger_record.pages_or_cursors_attempted) | set(declared)
                ),
                "search_result_count": sum(item.parsed_record_count for item in results),
                "unique_candidate_count": sum(item.parsed_record_count for item in results),
                "source_response_artifacts": sorted(
                    artifacts.values(), key=lambda item: item.artifact_id
                ),
                "completion_reason": proof.completion_reason,
                "error_classification": None if proof.completed else "partial_provider_retrieval",
                "retry_provenance": [
                    *ledger_record.retry_provenance,
                    *(
                        f"{attempt.resource_id}:attempt-{attempt.attempt_number}:{attempt.outcome}"
                        for result in results
                        for attempt in result.transport_attempts
                        if attempt.attempt_number > 1
                    ),
                ],
                "logical_tool_call_count": ledger_record.logical_tool_call_count + 1,
                "scientific_source_request_count": (
                    ledger_record.scientific_source_request_count + source_request_count
                ),
                "transport_attempt_count": (
                    ledger_record.transport_attempt_count + transport_attempt_count
                ),
            }
        )
        return ProviderExecutionOutcome(
            task=task,
            ledger_record=updated_record,
            page_or_file_results=results,
            completion_proof=proof,
            cache_manifest=cache_manifest,
            normalized_resources=normalized,
            logical_tool_call_count=updated_record.logical_tool_call_count,
            scientific_source_request_count=updated_record.scientific_source_request_count,
            transport_attempt_count=updated_record.transport_attempt_count,
        )
