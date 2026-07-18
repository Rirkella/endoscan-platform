"""Development-only zero-LLM probe over the production GEO validation implementation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import ArtifactDescriptor, ToolInvocation, WorkflowState
from .discovery_tools import DiscoveryToolService, GeoAccessionsInput
from .repository import deterministic_id
from .source_cache import CACHE_POLICY_VERSION, CachedSourceResponse, SourceResponseCache


class ProbeArtifactStore:
    """Content-addressed source artifacts with no workflow database side effects."""

    def __init__(self, root: Path, *, maximum_bytes: int = 2 * 1024 * 1024):
        self.root = (Path(root).resolve() / "source-probes").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.maximum_bytes = maximum_bytes
        self._records: dict[str, tuple[ArtifactDescriptor, bytes]] = {}

    def put_bytes(self, *, content: bytes, mime_type: str, logical_name: str, **kwargs):
        if len(content) > self.maximum_bytes:
            raise ValueError("Probe source artifact exceeded the configured size limit.")
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / "sha256" / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(prefix="probe-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temporary).replace(target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        artifact_id = deterministic_id("probe-art", logical_name, digest)
        record = ArtifactDescriptor(
            id=artifact_id,
            workflow_id="source-probe",
            step_id=None,
            sha256=digest,
            size_bytes=len(content),
            mime_type=mime_type,
            artifact_type=str(kwargs.get("artifact_type", "scientific_source_raw")),
            logical_name=logical_name,
            producer=str(kwargs.get("producer", "geo-validation-probe")),
            original_source=kwargs.get("original_source"),
            created_at=datetime.now(UTC),
        )
        self._records[artifact_id] = (record, content)
        return record

    def get(self, artifact_id: str):
        return self._records[artifact_id]


class ProbeSourceResponseCache:
    """Small bounded cache implementing the production cache contract without DB rows."""

    def __init__(self, *, ttl_seconds: int = 86_400):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, CachedSourceResponse] = {}

    def get(self, tool_name: str, arguments: dict[str, Any], **kwargs):
        key = SourceResponseCache.key(
            tool_name,
            arguments,
            source_version=kwargs.get("source_version"),
            policy_version=CACHE_POLICY_VERSION,
        )
        item = self._items.get(key)
        if item is None or item.expires_at <= datetime.now(UTC):
            return None
        return item

    def put(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        source_url: str,
        content_hash: str,
        parsed_output: dict[str, Any],
        raw_artifact_id: str,
        http_metadata: dict[str, Any],
        source_version: str | None = None,
    ) -> CachedSourceResponse:
        key = SourceResponseCache.key(
            tool_name,
            arguments,
            source_version=source_version,
            policy_version=CACHE_POLICY_VERSION,
        )
        retrieved = datetime.now(UTC)
        item = CachedSourceResponse(
            cache_key=key,
            parsed_output=parsed_output,
            raw_artifact_id=raw_artifact_id,
            source_url=source_url,
            content_hash=content_hash,
            retrieved_at=retrieved,
            expires_at=retrieved + timedelta(seconds=self.ttl_seconds),
            fresh=True,
            http_metadata=http_metadata,
        )
        self._items[key] = item
        return item


class GeoValidationProbe:
    def __init__(
        self,
        *,
        artifact_root: Path,
        source_client,
        ttl_seconds: int,
        ncbi_email: str | None = None,
        ncbi_api_key: str | None = None,
    ):
        self.artifacts = ProbeArtifactStore(artifact_root)
        self.cache = ProbeSourceResponseCache(ttl_seconds=ttl_seconds)
        self.service = DiscoveryToolService(
            self.cache,
            self.artifacts,
            source_client,
            ncbi_email=ncbi_email,
            ncbi_api_key=ncbi_api_key,
        )

    def run(self, accessions: list[str]) -> dict[str, Any]:
        request = GeoAccessionsInput(accessions=accessions)
        result = self.service.validate_geo_accessions(
            request,
            ToolInvocation(
                tool_name="validate_geo_accessions",
                arguments=request.model_dump(mode="json"),
                workflow_id="source-probe",
                step_id=None,
                workflow_stage=WorkflowState.DISCOVERING_DATA,
                permission_scope=["source:geo:read"],
                run_context={"run_mode": "live", "refresh_source_metadata": False},
                idempotency_key="geo-validation-probe",
            ),
        )
        return {
            "schema_version": "1.0.0",
            "probe_type": "geo_validation",
            "openai_calls": 0,
            "workflow_builds_created": 0,
            "agent_runs_created": 0,
            **result.model_dump(mode="json"),
        }
