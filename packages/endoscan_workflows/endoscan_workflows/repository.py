"""Transactional repository helpers, event chaining, and defensive JSON loading."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import SCHEMA_VERSION
from .errors import WorkflowConflict, WorkflowNotFound
from .models import EndpointBuildRow, WorkflowEventRow

NAMESPACE = uuid.UUID("5c809fa2-14ea-4f65-b109-02dcd0ca7ab7")
logger = logging.getLogger("endoscan.workflow")
ContractT = TypeVar("ContractT", bound=BaseModel)


def deterministic_id(prefix: str, *parts: str) -> str:
    value = ":".join((prefix, *parts))
    return f"{prefix}-{uuid.uuid5(NAMESPACE, value)}"


def utc_text(value: datetime | None = None) -> str:
    dt = value or datetime.now(UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def canonical_json(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def versioned_payload(**payload: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, **payload}


def load_versioned_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowConflict("Persisted structured payload is not valid JSON.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowConflict("Persisted structured payload has an unsupported schema version.")
    return value


def load_contract(raw: str, model: type[ContractT]) -> ContractT:
    try:
        return model.model_validate(load_versioned_json(raw))
    except ValidationError as exc:
        raise WorkflowConflict("Persisted structured payload failed defensive validation.") from exc


def require_build(session: Session, workflow_id: str) -> EndpointBuildRow:
    row = session.get(EndpointBuildRow, workflow_id)
    if row is None:
        raise WorkflowNotFound("Endpoint build was not found.")
    return row


def append_event(
    session: Session,
    build: EndpointBuildRow,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    from_state: str | None = None,
    to_state: str | None = None,
) -> WorkflowEventRow:
    existing = session.scalar(
        select(WorkflowEventRow).where(
            WorkflowEventRow.workflow_id == build.id,
            WorkflowEventRow.idempotency_key == idempotency_key,
        )
    )
    normalized = versioned_payload(**payload)
    if existing is not None:
        if (
            existing.event_type != event_type
            or load_versioned_json(existing.payload_json) != normalized
        ):
            raise WorkflowConflict("Idempotency key was already used for a different mutation.")
        return existing

    previous = session.scalar(
        select(WorkflowEventRow)
        .where(WorkflowEventRow.workflow_id == build.id)
        .order_by(WorkflowEventRow.sequence.desc())
        .limit(1)
    )
    sequence = (
        int(
            session.scalar(
                select(func.coalesce(func.max(WorkflowEventRow.sequence), 0)).where(
                    WorkflowEventRow.workflow_id == build.id
                )
            )
            or 0
        )
        + 1
    )
    previous_hash = previous.event_hash if previous else None
    digest_payload = canonical_json(
        {
            "workflow_id": build.id,
            "sequence": sequence,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "from_state": from_state,
            "to_state": to_state,
            "idempotency_key": idempotency_key,
            "payload": normalized,
            "previous_event_hash": previous_hash,
        }
    )
    event_hash = hashlib.sha256(digest_payload.encode()).hexdigest()
    event = WorkflowEventRow(
        id=deterministic_id("evt", build.id, str(sequence), event_hash),
        workflow_id=build.id,
        sequence=sequence,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        from_state=from_state,
        to_state=to_state,
        idempotency_key=idempotency_key,
        payload_json=canonical_json(normalized),
        previous_event_hash=previous_hash,
        event_hash=event_hash,
        created_at=utc_text(),
    )
    session.add(event)
    session.flush()
    logger.info(
        canonical_json(
            {
                "event_type": event_type,
                "workflow_id": build.id,
                "event_id": event.id,
                "actor_type": actor_type,
                "from_state": from_state,
                "to_state": to_state,
                "status": "recorded",
            }
        )
    )
    return event
