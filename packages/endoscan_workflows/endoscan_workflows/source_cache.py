"""Persistent cache for bounded scientific source responses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from .database import WorkflowDatabase
from .models import ArtifactRow, SourceCacheRow
from .repository import canonical_json, parse_utc
from .source_governor import current_source_request_context

CACHE_POLICY_VERSION = "source-policy-v2-geo-text"


class SourceCacheIntegrityError(RuntimeError):
    """A cache row may never outlive or invent its immutable artifact provenance."""


@dataclass(frozen=True)
class CachedSourceResponse:
    cache_key: str
    parsed_output: dict[str, Any]
    raw_artifact_id: str
    source_url: str
    content_hash: str
    retrieved_at: datetime
    expires_at: datetime
    fresh: bool
    http_metadata: dict[str, Any]
    policy_version: str


class SourceResponseCache:
    def __init__(self, database: WorkflowDatabase, *, ttl_seconds: int = 86_400):
        self.database = database
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def key(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        source_version: str | None = None,
        policy_version: str = CACHE_POLICY_VERSION,
    ) -> str:
        payload = canonical_json(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "source_version": source_version,
                "policy_version": policy_version,
            }
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        source_version: str | None = None,
        allow_stale: bool = False,
    ) -> CachedSourceResponse | None:
        key = self.key(tool_name, arguments, source_version=source_version)
        with self.database.session() as session:
            row = session.get(SourceCacheRow, key)
            if row is None or row.policy_version != CACHE_POLICY_VERSION:
                return None
            artifact = session.get(ArtifactRow, row.raw_artifact_id)
            if artifact is None:
                raise SourceCacheIntegrityError(
                    "Source cache provenance artifact is missing from the workflow database."
                )
            if artifact.sha256 != row.content_hash:
                raise SourceCacheIntegrityError(
                    "Source cache content hash does not match its provenance artifact."
                )
            expires_at = parse_utc(row.expires_at)
            fresh = expires_at > datetime.now(UTC)
            if not fresh and not allow_stale:
                return None
            context = current_source_request_context()
            if context is not None:
                context.cache_hit(tool_name=tool_name, url=row.source_url)
            return CachedSourceResponse(
                cache_key=row.cache_key,
                parsed_output=json.loads(row.parsed_output_json),
                raw_artifact_id=row.raw_artifact_id,
                source_url=row.source_url,
                content_hash=row.content_hash,
                retrieved_at=parse_utc(row.retrieved_at),
                expires_at=expires_at,
                fresh=fresh,
                http_metadata=json.loads(row.http_metadata_json),
                policy_version=row.policy_version,
            )

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
        key = self.key(tool_name, arguments, source_version=source_version)
        retrieved = datetime.now(UTC)
        expires = retrieved + timedelta(seconds=self.ttl_seconds)
        with self.database.session() as session:
            artifact = session.get(ArtifactRow, raw_artifact_id)
            if artifact is None:
                raise SourceCacheIntegrityError(
                    "Source cache provenance artifact must be persisted before the cache row."
                )
            if artifact.sha256 != content_hash:
                raise SourceCacheIntegrityError(
                    "Source cache content hash must match the persisted provenance artifact."
                )
            row = session.get(SourceCacheRow, key)
            values = {
                "tool_name": tool_name,
                "normalized_arguments_json": canonical_json(arguments),
                "policy_version": CACHE_POLICY_VERSION,
                "source_version": source_version,
                "source_url": source_url,
                "http_metadata_json": canonical_json(http_metadata),
                "content_hash": content_hash,
                "parsed_output_json": canonical_json(parsed_output),
                "raw_artifact_id": raw_artifact_id,
                "retrieved_at": retrieved.isoformat(),
                "expires_at": expires.isoformat(),
            }
            if row is None:
                row = SourceCacheRow(cache_key=key, **values)
                session.add(row)
            else:
                for name, value in values.items():
                    setattr(row, name, value)
        return CachedSourceResponse(
            cache_key=key,
            parsed_output=parsed_output,
            raw_artifact_id=raw_artifact_id,
            source_url=source_url,
            content_hash=content_hash,
            retrieved_at=retrieved,
            expires_at=expires,
            fresh=True,
            http_metadata=http_metadata,
            policy_version=CACHE_POLICY_VERSION,
        )

    def invalidate(self, *, tool_name: str | None = None) -> int:
        with self.database.session() as session:
            statement = delete(SourceCacheRow)
            if tool_name:
                statement = statement.where(SourceCacheRow.tool_name == tool_name)
            result = session.execute(statement)
            return int(result.rowcount or 0)
