"""Durable, build-global governance for scientific-source transport attempts."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .database import WorkflowDatabase
from .models import (
    ScientificSourceRequestAttemptRow,
    ScientificSourceRequestBudgetRow,
)
from .repository import deterministic_id, utc_text
from .source_scheduler import (
    BuildFairRequestScheduler,
    ScientificSourceTaskAllocationExhausted,
)


class ScientificSourceBudgetExhausted(RuntimeError):
    """A transport was rejected before leaving the process."""


class ScientificSourceExecutionCancelled(RuntimeError):
    """A transport was prevented after the owning task was cancelled."""


@dataclass(frozen=True)
class SourceRequestAccounting:
    workflow_id: str
    discovery_round: int
    maximum_requests: int
    reserved_requests: int
    completed_transport_attempts: int
    failed_transport_attempts: int
    cache_hits: int
    blocked_requests: int
    prevented_by_cancellation: int

    @property
    def remaining_requests(self) -> int:
        return max(self.maximum_requests - self.reserved_requests, 0)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "workflow_id": self.workflow_id,
            "discovery_round": self.discovery_round,
            "allowed_global_request_budget": self.maximum_requests,
            "reserved_requests": self.reserved_requests,
            "completed_transport_attempts": self.completed_transport_attempts,
            "failed_transport_attempts": self.failed_transport_attempts,
            "cache_hits": self.cache_hits,
            "blocked_requests_after_budget_exhaustion": self.blocked_requests,
            "requests_prevented_by_cancellation": self.prevented_by_cancellation,
            "remaining_requests": self.remaining_requests,
        }


class SourceTaskCancellation:
    """Cooperative task cancellation shared by transport and pagination code."""

    def __init__(self, *, deadline_monotonic: float) -> None:
        self.deadline_monotonic = deadline_monotonic
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._cancelled_at_monotonic: float | None = None

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason[:120]
            self._cancelled_at_monotonic = time.monotonic()
            self._event.set()

    def check_deadline(self) -> None:
        if not self._event.is_set() and time.monotonic() >= self.deadline_monotonic:
            self.cancel("tool_timeout")

    @property
    def cancelled(self) -> bool:
        self.check_deadline()
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def cancelled_at_monotonic(self) -> float | None:
        return self._cancelled_at_monotonic


class ScientificSourceRequestGovernor:
    """Atomically reserve and reconcile one request budget per workflow round."""

    def __init__(self, database: WorkflowDatabase) -> None:
        self.database = database
        self.scheduler = BuildFairRequestScheduler(database)

    @staticmethod
    def _budget_id(workflow_id: str, discovery_round: int) -> str:
        return deterministic_id("source-budget", workflow_id, str(discovery_round))

    def ensure_budget(self, workflow_id: str, discovery_round: int, maximum_requests: int) -> str:
        if maximum_requests < 0:
            raise ValueError("maximum scientific-source requests cannot be negative")
        budget_id = self._budget_id(workflow_id, discovery_round)
        for _attempt in range(2):
            try:
                with self.database.session() as session:
                    row = session.get(ScientificSourceRequestBudgetRow, budget_id)
                    if row is None:
                        now = utc_text()
                        session.add(
                            ScientificSourceRequestBudgetRow(
                                id=budget_id,
                                workflow_id=workflow_id,
                                discovery_round=discovery_round,
                                maximum_requests=maximum_requests,
                                reserved_requests=0,
                                completed_transport_attempts=0,
                                failed_transport_attempts=0,
                                cache_hits=0,
                                blocked_requests=0,
                                prevented_by_cancellation=0,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    elif row.maximum_requests != maximum_requests:
                        raise ValueError(
                            "A resumed workflow cannot receive a different scientific-source "
                            "request budget."
                        )
                return budget_id
            except IntegrityError:
                continue
        raise RuntimeError("Unable to initialize the scientific-source request budget.")

    @staticmethod
    def _safe_request(url: str, params: dict[str, str | int] | None) -> tuple[str, str, str]:
        parsed = urlparse(url)
        host = (parsed.hostname or "unknown").lower().rstrip(".")[:253]
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        path = path[:500]
        material = url
        if params:
            material = f"{url}?{urlencode(sorted((str(k), str(v)) for k, v in params.items()))}"
        return host, path, hashlib.sha256(material.encode()).hexdigest()

    def reserve(
        self,
        *,
        workflow_id: str,
        discovery_round: int,
        maximum_requests: int,
        task_id: str,
        tool_name: str,
        url: str,
        params: dict[str, str | int] | None,
    ) -> str:
        budget_id = self.ensure_budget(workflow_id, discovery_round, maximum_requests)
        host, path, fingerprint = self._safe_request(url, params)
        now = utc_text()
        blocked = False
        attempt_id: str | None = None
        scheduler_consumed = self.scheduler.acquire_if_configured(
            workflow_id,
            discovery_round,
            task_id,
        )
        with self.database.session() as session:
            sequence = session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(
                    ScientificSourceRequestBudgetRow.id == budget_id,
                    ScientificSourceRequestBudgetRow.reserved_requests
                    < ScientificSourceRequestBudgetRow.maximum_requests,
                )
                .values(
                    reserved_requests=ScientificSourceRequestBudgetRow.reserved_requests + 1,
                    updated_at=now,
                )
                .returning(ScientificSourceRequestBudgetRow.reserved_requests)
            ).scalar_one_or_none()
            if sequence is None:
                session.execute(
                    update(ScientificSourceRequestBudgetRow)
                    .where(ScientificSourceRequestBudgetRow.id == budget_id)
                    .values(
                        blocked_requests=ScientificSourceRequestBudgetRow.blocked_requests + 1,
                        updated_at=now,
                    )
                )
                session.add(
                    ScientificSourceRequestAttemptRow(
                        id=f"source-attempt-{uuid4()}",
                        budget_id=budget_id,
                        workflow_id=workflow_id,
                        discovery_round=discovery_round,
                        task_id=task_id,
                        tool_name=tool_name,
                        reservation_sequence=None,
                        request_fingerprint=fingerprint,
                        source_host=host,
                        safe_url_path=path,
                        outcome="blocked_budget_exhausted",
                        request_left_process=0,
                        started_at=None,
                        completed_at=now,
                        http_status=None,
                        final_approved_host=None,
                        error_category="global_request_budget_exhausted",
                        created_at=now,
                    )
                )
                blocked = True
            else:
                attempt_id = deterministic_id("source-attempt", budget_id, str(sequence))
                session.add(
                    ScientificSourceRequestAttemptRow(
                        id=attempt_id,
                        budget_id=budget_id,
                        workflow_id=workflow_id,
                        discovery_round=discovery_round,
                        task_id=task_id,
                        tool_name=tool_name,
                        reservation_sequence=int(sequence),
                        request_fingerprint=fingerprint,
                        source_host=host,
                        safe_url_path=path,
                        outcome="reserved",
                        request_left_process=0,
                        started_at=None,
                        completed_at=None,
                        http_status=None,
                        final_approved_host=None,
                        error_category=None,
                        created_at=now,
                    )
                )
        if blocked:
            if scheduler_consumed:
                self.scheduler.release_consumption(workflow_id, discovery_round, task_id)
            raise ScientificSourceBudgetExhausted(
                "The global scientific-source request budget is exhausted."
            )
        if attempt_id is None:
            raise RuntimeError("Scientific-source request reservation was not persisted.")
        return attempt_id

    def mark_started(self, attempt_id: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(ScientificSourceRequestAttemptRow)
                .where(
                    ScientificSourceRequestAttemptRow.id == attempt_id,
                    ScientificSourceRequestAttemptRow.outcome == "reserved",
                )
                .values(
                    outcome="in_flight",
                    request_left_process=1,
                    started_at=utc_text(),
                )
            )

    def mark_completed(
        self,
        attempt_id: str,
        *,
        http_status: int,
        final_approved_host: str | None,
    ) -> None:
        now = utc_text()
        with self.database.session() as session:
            attempt = session.get(ScientificSourceRequestAttemptRow, attempt_id)
            if attempt is None or attempt.outcome not in {"reserved", "in_flight"}:
                return
            attempt.outcome = "completed"
            attempt.request_left_process = 1
            attempt.completed_at = now
            attempt.http_status = http_status
            attempt.final_approved_host = final_approved_host
            session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(ScientificSourceRequestBudgetRow.id == attempt.budget_id)
                .values(
                    completed_transport_attempts=(
                        ScientificSourceRequestBudgetRow.completed_transport_attempts + 1
                    ),
                    updated_at=now,
                )
            )

    def mark_failed(self, attempt_id: str, *, error_category: str) -> None:
        now = utc_text()
        with self.database.session() as session:
            attempt = session.get(ScientificSourceRequestAttemptRow, attempt_id)
            if attempt is None or attempt.outcome not in {"reserved", "in_flight"}:
                return
            attempt.outcome = "failed"
            attempt.request_left_process = 1
            attempt.completed_at = now
            attempt.error_category = error_category[:120]
            session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(ScientificSourceRequestBudgetRow.id == attempt.budget_id)
                .values(
                    failed_transport_attempts=(
                        ScientificSourceRequestBudgetRow.failed_transport_attempts + 1
                    ),
                    updated_at=now,
                )
            )

    def record_cache_hit(
        self,
        *,
        workflow_id: str,
        discovery_round: int,
        maximum_requests: int,
        task_id: str,
        tool_name: str,
        url: str,
    ) -> None:
        budget_id = self.ensure_budget(workflow_id, discovery_round, maximum_requests)
        host, path, fingerprint = self._safe_request(url, None)
        now = utc_text()
        with self.database.session() as session:
            attempt_id = deterministic_id(
                "source-cache-observation",
                budget_id,
                task_id,
                tool_name,
                fingerprint,
            )
            if session.get(ScientificSourceRequestAttemptRow, attempt_id) is not None:
                return
            session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(ScientificSourceRequestBudgetRow.id == budget_id)
                .values(
                    cache_hits=ScientificSourceRequestBudgetRow.cache_hits + 1,
                    updated_at=now,
                )
            )
            session.add(
                ScientificSourceRequestAttemptRow(
                    id=attempt_id,
                    budget_id=budget_id,
                    workflow_id=workflow_id,
                    discovery_round=discovery_round,
                    task_id=task_id,
                    tool_name=tool_name,
                    reservation_sequence=None,
                    request_fingerprint=fingerprint,
                    source_host=host,
                    safe_url_path=path,
                    outcome="cache_hit",
                    request_left_process=0,
                    started_at=None,
                    completed_at=now,
                    http_status=None,
                    final_approved_host=host,
                    error_category=None,
                    created_at=now,
                )
            )

    def record_prevented(
        self,
        *,
        workflow_id: str,
        discovery_round: int,
        maximum_requests: int,
        task_id: str,
        tool_name: str,
        url: str,
        reason: str,
    ) -> None:
        budget_id = self.ensure_budget(workflow_id, discovery_round, maximum_requests)
        host, path, fingerprint = self._safe_request(url, None)
        now = utc_text()
        with self.database.session() as session:
            attempt_id = deterministic_id(
                "source-cancellation-observation",
                budget_id,
                task_id,
                tool_name,
                fingerprint,
                reason,
            )
            if session.get(ScientificSourceRequestAttemptRow, attempt_id) is not None:
                return
            session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(ScientificSourceRequestBudgetRow.id == budget_id)
                .values(
                    prevented_by_cancellation=(
                        ScientificSourceRequestBudgetRow.prevented_by_cancellation + 1
                    ),
                    updated_at=now,
                )
            )
            session.add(
                ScientificSourceRequestAttemptRow(
                    id=attempt_id,
                    budget_id=budget_id,
                    workflow_id=workflow_id,
                    discovery_round=discovery_round,
                    task_id=task_id,
                    tool_name=tool_name,
                    reservation_sequence=None,
                    request_fingerprint=fingerprint,
                    source_host=host,
                    safe_url_path=path,
                    outcome="prevented_by_cancellation",
                    request_left_process=0,
                    started_at=None,
                    completed_at=now,
                    http_status=None,
                    final_approved_host=None,
                    error_category=reason[:120],
                    created_at=now,
                )
            )

    def accounting(
        self,
        workflow_id: str,
        discovery_round: int,
        *,
        task_id: str | None = None,
    ) -> SourceRequestAccounting | None:
        budget_id = self._budget_id(workflow_id, discovery_round)
        with self.database.session() as session:
            budget = session.get(ScientificSourceRequestBudgetRow, budget_id)
            if budget is None:
                return None
            statement = select(
                func.count().filter(
                    ScientificSourceRequestAttemptRow.reservation_sequence.is_not(None)
                ),
                func.count().filter(ScientificSourceRequestAttemptRow.outcome == "completed"),
                func.count().filter(ScientificSourceRequestAttemptRow.outcome == "failed"),
                func.count().filter(ScientificSourceRequestAttemptRow.outcome == "cache_hit"),
                func.count().filter(
                    ScientificSourceRequestAttemptRow.outcome == "blocked_budget_exhausted"
                ),
                func.count().filter(
                    ScientificSourceRequestAttemptRow.outcome == "prevented_by_cancellation"
                ),
            ).where(ScientificSourceRequestAttemptRow.budget_id == budget_id)
            if task_id is not None:
                statement = statement.where(ScientificSourceRequestAttemptRow.task_id == task_id)
            counts = session.execute(statement).one()
            return SourceRequestAccounting(
                workflow_id=workflow_id,
                discovery_round=discovery_round,
                maximum_requests=budget.maximum_requests,
                reserved_requests=int(counts[0] or 0),
                completed_transport_attempts=int(counts[1] or 0),
                failed_transport_attempts=int(counts[2] or 0),
                cache_hits=int(counts[3] or 0),
                blocked_requests=int(counts[4] or 0),
                prevented_by_cancellation=int(counts[5] or 0),
            )

    def reconcile(self, workflow_id: str, discovery_round: int) -> SourceRequestAccounting | None:
        accounting = self.accounting(workflow_id, discovery_round)
        if accounting is None:
            return None
        budget_id = self._budget_id(workflow_id, discovery_round)
        with self.database.session() as session:
            session.execute(
                update(ScientificSourceRequestBudgetRow)
                .where(ScientificSourceRequestBudgetRow.id == budget_id)
                .values(
                    reserved_requests=accounting.reserved_requests,
                    completed_transport_attempts=accounting.completed_transport_attempts,
                    failed_transport_attempts=accounting.failed_transport_attempts,
                    cache_hits=accounting.cache_hits,
                    blocked_requests=accounting.blocked_requests,
                    prevented_by_cancellation=accounting.prevented_by_cancellation,
                    updated_at=utc_text(),
                )
            )
        return accounting


@dataclass
class SourceRequestExecutionContext:
    governor: ScientificSourceRequestGovernor
    workflow_id: str
    discovery_round: int
    maximum_requests: int
    task_id: str
    cancellation: SourceTaskCancellation

    def before_transport(
        self,
        *,
        tool_name: str,
        url: str,
        params: dict[str, str | int] | None,
    ) -> str:
        if self.cancellation.cancelled:
            self.governor.record_prevented(
                workflow_id=self.workflow_id,
                discovery_round=self.discovery_round,
                maximum_requests=self.maximum_requests,
                task_id=self.task_id,
                tool_name=tool_name,
                url=url,
                reason=self.cancellation.reason or "task_cancelled",
            )
            raise ScientificSourceExecutionCancelled(
                "The scientific-source request was prevented because its task was cancelled."
            )
        try:
            attempt_id = self.governor.reserve(
                workflow_id=self.workflow_id,
                discovery_round=self.discovery_round,
                maximum_requests=self.maximum_requests,
                task_id=self.task_id,
                tool_name=tool_name,
                url=url,
                params=params,
            )
        except ScientificSourceBudgetExhausted:
            self.cancellation.cancel("global_request_budget_exhausted")
            raise
        except ScientificSourceTaskAllocationExhausted:
            self.cancellation.cancel("task_request_allocation_exhausted")
            raise
        if self.cancellation.cancelled:
            self.governor.record_prevented(
                workflow_id=self.workflow_id,
                discovery_round=self.discovery_round,
                maximum_requests=self.maximum_requests,
                task_id=self.task_id,
                tool_name=tool_name,
                url=url,
                reason=self.cancellation.reason or "task_cancelled",
            )
            raise ScientificSourceExecutionCancelled(
                "The scientific-source request was prevented because its task was cancelled."
            )
        self.governor.mark_started(attempt_id)
        return attempt_id

    def cache_hit(self, *, tool_name: str, url: str) -> None:
        self.governor.record_cache_hit(
            workflow_id=self.workflow_id,
            discovery_round=self.discovery_round,
            maximum_requests=self.maximum_requests,
            task_id=self.task_id,
            tool_name=tool_name,
            url=url,
        )


_CURRENT_SOURCE_REQUEST_CONTEXT: ContextVar[SourceRequestExecutionContext | None] = ContextVar(
    "endoscan_source_request_context", default=None
)


def current_source_request_context() -> SourceRequestExecutionContext | None:
    return _CURRENT_SOURCE_REQUEST_CONTEXT.get()


@contextmanager
def activate_source_request_context(
    context: SourceRequestExecutionContext | None,
) -> Iterator[None]:
    token = _CURRENT_SOURCE_REQUEST_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_SOURCE_REQUEST_CONTEXT.reset(token)
