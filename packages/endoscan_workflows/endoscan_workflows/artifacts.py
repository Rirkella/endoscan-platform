"""Bounded, content-addressed local artifact storage with DB metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import SCHEMA_VERSION, ArtifactDescriptor
from .database import WorkflowDatabase
from .errors import ArtifactError, ArtifactIntegrityError, ArtifactTooLarge, WorkflowNotFound
from .models import ArtifactRow, WorkflowStepRow
from .repository import (
    append_event,
    canonical_json,
    deterministic_id,
    parse_utc,
    require_build,
    utc_text,
    versioned_payload,
)

ALLOWED_MIME_TYPES = {
    "application/json",
    "text/plain",
    "text/csv",
    "text/markdown",
    "application/octet-stream",
}


class LocalArtifactStore:
    def __init__(
        self,
        database: WorkflowDatabase,
        root: Path,
        *,
        maximum_bytes: int = 2 * 1024 * 1024,
    ):
        self.database = database
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.maximum_bytes = maximum_bytes

    def _path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ArtifactError("Artifact digest is invalid.")
        path = (self.root / "sha256" / sha256[:2] / sha256).resolve()
        if self.root not in path.parents:
            raise ArtifactError("Artifact path escaped the configured storage root.")
        return path

    def _write_blob(self, content: bytes, sha256: str) -> str:
        target = self._path(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
                raise ArtifactIntegrityError(
                    "Existing artifact blob failed integrity verification."
                )
            return target.relative_to(self.root).as_posix()
        descriptor, temp_name = tempfile.mkstemp(prefix="artifact-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temp_name).replace(target)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return target.relative_to(self.root).as_posix()

    def _put_bytes(
        self,
        session: Session,
        *,
        workflow_id: str,
        content: bytes,
        mime_type: str,
        artifact_type: str,
        logical_name: str,
        producer: str,
        idempotency_key: str,
        step_id: str | None = None,
        original_source: str | None = None,
    ) -> ArtifactDescriptor:
        if len(content) > self.maximum_bytes:
            raise ArtifactTooLarge(f"Artifact exceeds the {self.maximum_bytes}-byte Phase-0 limit.")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise ArtifactError("Artifact MIME type is not allowed.")
        build = require_build(session, workflow_id)
        if step_id is not None and session.get(WorkflowStepRow, step_id) is None:
            raise WorkflowNotFound("Producer step was not found.")
        digest = hashlib.sha256(content).hexdigest()
        existing = session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.workflow_id == workflow_id,
                ArtifactRow.logical_name == logical_name,
                ArtifactRow.sha256 == digest,
            )
        )
        if existing is not None:
            return self._descriptor(existing)
        storage_key = self._write_blob(content, digest)
        created_at = utc_text()
        artifact = ArtifactRow(
            id=deterministic_id("art", workflow_id, logical_name, digest),
            workflow_id=workflow_id,
            step_id=step_id,
            sha256=digest,
            size_bytes=len(content),
            mime_type=mime_type,
            artifact_type=artifact_type,
            logical_name=logical_name,
            storage_key=storage_key,
            producer=producer,
            original_source=original_source,
            metadata_json=canonical_json(
                versioned_payload(
                    artifact_type=artifact_type,
                    logical_name=logical_name,
                    producer=producer,
                    original_source=original_source,
                )
            ),
            created_at=created_at,
        )
        session.add(artifact)
        session.flush()
        append_event(
            session,
            build,
            event_type="artifact.created",
            actor_type="system",
            actor_id=producer,
            idempotency_key=f"{idempotency_key}:artifact:{digest}",
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact_type,
                "logical_name": logical_name,
                "sha256": digest,
                "size_bytes": len(content),
                "step_id": step_id,
            },
            from_state=build.current_stage,
            to_state=build.current_stage,
        )
        return self._descriptor(artifact)

    def put_bytes(self, **kwargs) -> ArtifactDescriptor:
        with self.database.session() as session:
            return self._put_bytes(session, **kwargs)

    def put_json(self, *, value: dict, **kwargs) -> ArtifactDescriptor:
        payload = (
            value if value.get("schema_version") else {"schema_version": SCHEMA_VERSION, **value}
        )
        content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode()
        return self.put_bytes(content=content, mime_type="application/json", **kwargs)

    def get(self, artifact_id: str) -> tuple[ArtifactDescriptor, bytes]:
        with self.database.session() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                raise WorkflowNotFound("Artifact was not found.")
            descriptor = self._descriptor(row)
            content = self._path(row.sha256).read_bytes()
        if hashlib.sha256(content).hexdigest() != descriptor.sha256:
            raise ArtifactIntegrityError("Artifact failed integrity verification.")
        return descriptor, content

    def verify(self, artifact_id: str) -> bool:
        self.get(artifact_id)
        return True

    def list_artifacts(self, workflow_id: str) -> list[ArtifactDescriptor]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            rows = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.workflow_id == workflow_id)
                .order_by(ArtifactRow.created_at, ArtifactRow.id)
            ).all()
            return [self._descriptor(row) for row in rows]

    def attach_artifact_to_step(
        self, artifact_id: str, step_id: str, *, idempotency_key: str
    ) -> ArtifactDescriptor:
        with self.database.session() as session:
            row = session.get(ArtifactRow, artifact_id)
            step = session.get(WorkflowStepRow, step_id)
            if row is None or step is None or row.workflow_id != step.workflow_id:
                raise WorkflowNotFound("Artifact or compatible step was not found.")
            if row.step_id not in {None, step_id}:
                raise ArtifactError("Artifact is already attached to a different step.")
            if row.step_id is None:
                row.step_id = step_id
                build = require_build(session, row.workflow_id)
                append_event(
                    session,
                    build,
                    event_type="artifact.attached",
                    actor_type="system",
                    actor_id="artifact-store",
                    idempotency_key=idempotency_key,
                    payload={"artifact_id": artifact_id, "step_id": step_id},
                    from_state=build.current_stage,
                    to_state=build.current_stage,
                )
            return self._descriptor(row)

    def capability(self) -> dict[str, str | bool | int]:
        return {
            "available": self.root.is_dir() and os.access(self.root, os.W_OK),
            "backend": "local-content-addressed",
            "maximum_bytes": self.maximum_bytes,
        }

    @staticmethod
    def _descriptor(row: ArtifactRow) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            id=row.id,
            workflow_id=row.workflow_id,
            step_id=row.step_id,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            mime_type=row.mime_type,
            artifact_type=row.artifact_type,
            logical_name=row.logical_name,
            producer=row.producer,
            original_source=row.original_source,
            created_at=parse_utc(row.created_at),
        )
