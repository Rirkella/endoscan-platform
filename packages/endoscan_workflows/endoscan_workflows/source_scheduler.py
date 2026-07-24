"""Persistent fair scheduling above the build-global scientific request governor."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from .database import WorkflowDatabase
from .discovery_strategy import DiscoveryPlan, EvidenceRole, ProviderDiscoveryTask
from .models import (
    ScientificSourceAllocationBucketRow,
    ScientificSourceAllocationPolicyRow,
    ScientificSourceRequestAttemptRow,
    ScientificSourceTaskAllocationRow,
)
from .repository import deterministic_id, utc_text

FAIR_REQUEST_POLICY_VERSION = "1.0.0"


class ScientificSourceTaskAllocationExhausted(RuntimeError):
    """The task was stopped before transport at its persisted fair-share ceiling."""


@dataclass(frozen=True)
class TaskAllocationDecision:
    task_id: str
    provider: str
    evidence_role: str
    modality: str | None
    execution_phase: str
    sequence: int
    initial_allocation: int
    allocated_requests: int
    maximum_requests: int
    consumed_requests: int
    released_requests: int
    status: str
    prerequisite: dict[str, Any]

    @property
    def remaining_requests(self) -> int:
        return max(self.allocated_requests - self.consumed_requests, 0)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _source_family(task: ProviderDiscoveryTask) -> str:
    if task.provider == "lincs-l1000":
        return "lincs_l1000"
    if task.provider == "ncbi-geo":
        return "geo"
    if task.provider == "pubchem-compound":
        return "compound_identity"
    if task.provider == "ncbi-supporting-metadata":
        return "supporting_metadata"
    return task.provider


def _phase_and_sequence(
    task: ProviderDiscoveryTask,
    *,
    modality_order: dict[str, int],
    provider_order: dict[str, int],
) -> tuple[str, int]:
    if task.provider == "lincs-l1000":
        return "A_prerequisite_and_transcriptomic_readiness", 100
    if task.evidence_role is EvidenceRole.ACTIVITY:
        modality = modality_order.get(task.modality or "", 99)
        provider = provider_order.get(task.provider, 99)
        return "B_shallow_activity_breadth", 200 + modality * 20 + provider
    if task.evidence_role is EvidenceRole.IDENTITY:
        return "C_identity_bridge", 400
    if task.provider == "ncbi-geo":
        return "C_supplemental_transcriptomic_metadata", 500
    if task.evidence_role is EvidenceRole.SUPPORTING_METADATA:
        return "D_supporting_metadata", 600
    return "D_evidence_directed_expansion", 700 + provider_order.get(task.provider, 99)


def _allocation_at_80(task: ProviderDiscoveryTask) -> tuple[int, int]:
    if task.provider == "lincs-l1000":
        return 12, 16
    if task.provider == "pubchem-compound":
        return 10, 14
    if task.provider == "ncbi-geo":
        return 6, 8
    if task.provider == "ncbi-supporting-metadata":
        return 4, 4
    if task.provider == "pubchem-bioassay":
        return 5, 8
    if task.provider == "tox21":
        return 2, 3
    if task.provider == "toxcast":
        return 3, 3
    return 3, 5


def _scaled(value: int, maximum_requests: int) -> int:
    if value == 0 or maximum_requests == 0:
        return 0
    return max(1, math.floor((value * maximum_requests + 79) / 80))


def _bucket_caps(maximum_requests: int) -> dict[tuple[str, str], int]:
    values = {
        ("evidence_role", EvidenceRole.ACTIVITY.value): 42,
        ("evidence_role", EvidenceRole.TRANSCRIPTOMIC.value): 20,
        ("evidence_role", EvidenceRole.IDENTITY.value): 14,
        ("evidence_role", EvidenceRole.SUPPORTING_METADATA.value): 4,
        ("provider", "pubchem-bioassay"): 24,
        ("provider", "tox21"): 9,
        ("provider", "toxcast"): 9,
        ("provider", "lincs-l1000"): 16,
        ("provider", "pubchem-compound"): 14,
        ("provider", "ncbi-geo"): 8,
        ("provider", "ncbi-supporting-metadata"): 4,
        ("source_family", "pubchem-bioassay"): 24,
        ("source_family", "tox21"): 9,
        ("source_family", "toxcast"): 9,
        ("source_family", "lincs_l1000"): 16,
        ("source_family", "compound_identity"): 14,
        ("source_family", "geo"): 8,
        ("source_family", "supporting_metadata"): 4,
    }
    return {
        key: min(_scaled(value, maximum_requests), maximum_requests)
        for key, value in values.items()
    }


class BuildFairRequestScheduler:
    """Deterministic bounded-batch scheduler whose decisions survive restart."""

    def __init__(self, database: WorkflowDatabase) -> None:
        self.database = database

    @staticmethod
    def _policy_id(workflow_id: str, discovery_round: int) -> str:
        return deterministic_id("source-allocation-policy", workflow_id, str(discovery_round))

    def ensure_policy(
        self,
        plan: DiscoveryPlan,
        *,
        prerequisite_findings: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        policy_id = self._policy_id(plan.workflow_id, plan.discovery_round)
        maximum = plan.scientific_and_execution_budgets.maximum_scientific_source_requests
        modality_order = {value: index for index, value in enumerate(plan.requested_modalities)}
        provider_order = {
            "pubchem-bioassay": 0,
            "tox21": 1,
            "toxcast": 2,
            "lincs-l1000": 3,
            "pubchem-compound": 4,
            "ncbi-geo": 5,
            "ncbi-supporting-metadata": 6,
        }
        findings = prerequisite_findings or {}
        task_values: list[dict[str, Any]] = []
        for task in plan.provider_specific_query_tasks:
            phase, sequence = _phase_and_sequence(
                task,
                modality_order=modality_order,
                provider_order=provider_order,
            )
            initial_at_80, maximum_at_80 = _allocation_at_80(task)
            prerequisite = dict(findings.get(task.task_id) or {})
            blocked = prerequisite.get("status") == "prerequisite_blocked"
            task_values.append(
                {
                    "task_id": task.task_id,
                    "provider": task.provider,
                    "source_family": _source_family(task),
                    "evidence_role": task.evidence_role.value,
                    "modality": task.modality,
                    "execution_phase": phase,
                    "sequence": sequence,
                    "initial_allocation": 0 if blocked else _scaled(initial_at_80, maximum),
                    "maximum_requests": 0 if blocked else _scaled(maximum_at_80, maximum),
                    "status": "prerequisite_blocked" if blocked else "pending",
                    "prerequisite": prerequisite,
                }
            )
        task_values.sort(key=lambda item: (item["sequence"], item["task_id"]))
        initial_total = sum(int(item["initial_allocation"]) for item in task_values)
        shared_remainder = max(maximum - initial_total, 0)
        policy_document = {
            "policy_version": FAIR_REQUEST_POLICY_VERSION,
            "workflow_id": plan.workflow_id,
            "discovery_round": plan.discovery_round,
            "plan_fingerprint": plan.plan_fingerprint,
            "maximum_requests": maximum,
            "phases": [
                "A_prerequisite_and_transcriptomic_readiness",
                "B_shallow_activity_breadth",
                "C_identity_bridge",
                "C_supplemental_transcriptomic_metadata",
                "D_evidence_directed_expansion",
                "D_supporting_metadata",
            ],
            "tasks": task_values,
            "bucket_caps": [
                {"kind": kind, "key": key, "maximum_requests": value}
                for (kind, key), value in sorted(_bucket_caps(maximum).items())
            ],
            "initial_shared_remainder": shared_remainder,
            "single_task_share_before_breadth": "bounded by the persisted task maximum",
            "global_governor_remains_final_authority": True,
        }
        now = utc_text()
        with self.database.session() as session:
            existing = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            if existing is not None:
                if (
                    existing.policy_version != FAIR_REQUEST_POLICY_VERSION
                    or existing.plan_fingerprint != plan.plan_fingerprint
                    or existing.maximum_requests != maximum
                    or existing.policy_json != _canonical_json(policy_document)
                ):
                    raise ValueError(
                        "A resumed discovery round cannot receive a different "
                        "fair-allocation policy."
                    )
                return policy_id
            session.add(
                ScientificSourceAllocationPolicyRow(
                    id=policy_id,
                    workflow_id=plan.workflow_id,
                    discovery_round=plan.discovery_round,
                    policy_version=FAIR_REQUEST_POLICY_VERSION,
                    plan_fingerprint=plan.plan_fingerprint,
                    maximum_requests=maximum,
                    initial_shared_remainder=shared_remainder,
                    available_shared_remainder=shared_remainder,
                    policy_json=_canonical_json(policy_document),
                    created_at=now,
                    updated_at=now,
                )
            )
            # ORM relationships are intentionally absent on audit rows; flush the
            # parent explicitly so SQLite can validate child foreign keys.
            session.flush()
            for (kind, key), cap in sorted(_bucket_caps(maximum).items()):
                session.add(
                    ScientificSourceAllocationBucketRow(
                        id=deterministic_id("source-allocation-bucket", policy_id, kind, key),
                        policy_id=policy_id,
                        workflow_id=plan.workflow_id,
                        discovery_round=plan.discovery_round,
                        bucket_kind=kind,
                        bucket_key=key,
                        maximum_requests=cap,
                        consumed_requests=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            for item in task_values:
                session.add(
                    ScientificSourceTaskAllocationRow(
                        id=deterministic_id(
                            "source-task-allocation", policy_id, str(item["task_id"])
                        ),
                        policy_id=policy_id,
                        workflow_id=plan.workflow_id,
                        discovery_round=plan.discovery_round,
                        task_id=str(item["task_id"]),
                        provider=str(item["provider"]),
                        source_family=str(item["source_family"]),
                        evidence_role=str(item["evidence_role"]),
                        modality=item["modality"],
                        execution_phase=str(item["execution_phase"]),
                        sequence=int(item["sequence"]),
                        initial_allocation=int(item["initial_allocation"]),
                        allocated_requests=int(item["initial_allocation"]),
                        maximum_requests=int(item["maximum_requests"]),
                        consumed_requests=0,
                        blocked_requests=0,
                        released_requests=0,
                        status=str(item["status"]),
                        terminal_outcome=None,
                        prerequisite_json=_canonical_json(item["prerequisite"]),
                        completion_reason=(
                            str(item["prerequisite"].get("remediation") or "")
                            if item["status"] == "prerequisite_blocked"
                            else None
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
        return policy_id

    def _allocation(
        self, workflow_id: str, discovery_round: int, task_id: str
    ) -> ScientificSourceTaskAllocationRow | None:
        policy_id = self._policy_id(workflow_id, discovery_round)
        with self.database.session() as session:
            return session.execute(
                select(ScientificSourceTaskAllocationRow).where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id == task_id,
                )
            ).scalar_one_or_none()

    @staticmethod
    def _decision(row: ScientificSourceTaskAllocationRow) -> TaskAllocationDecision:
        return TaskAllocationDecision(
            task_id=row.task_id,
            provider=row.provider,
            evidence_role=row.evidence_role,
            modality=row.modality,
            execution_phase=row.execution_phase,
            sequence=row.sequence,
            initial_allocation=row.initial_allocation,
            allocated_requests=row.allocated_requests,
            maximum_requests=row.maximum_requests,
            consumed_requests=row.consumed_requests,
            released_requests=row.released_requests,
            status=row.status,
            prerequisite=json.loads(row.prerequisite_json),
        )

    def decision(
        self, workflow_id: str, discovery_round: int, task_id: str
    ) -> TaskAllocationDecision | None:
        row = self._allocation(workflow_id, discovery_round, task_id)
        return self._decision(row) if row is not None else None

    def next_task_id(
        self,
        workflow_id: str,
        discovery_round: int,
        pending_task_ids: set[str],
    ) -> str | None:
        policy_id = self._policy_id(workflow_id, discovery_round)
        if not pending_task_ids:
            return None
        with self.database.session() as session:
            rows = session.execute(
                select(ScientificSourceTaskAllocationRow)
                .where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id.in_(pending_task_ids),
                )
                .order_by(
                    ScientificSourceTaskAllocationRow.sequence,
                    ScientificSourceTaskAllocationRow.task_id,
                )
            ).scalars()
            row = next(iter(rows), None)
            return row.task_id if row is not None else None

    def prepare_task(
        self, workflow_id: str, discovery_round: int, task_id: str
    ) -> TaskAllocationDecision:
        policy_id = self._policy_id(workflow_id, discovery_round)
        now = utc_text()
        with self.database.session() as session:
            policy = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            if policy is None:
                raise ValueError("Fair request-allocation policy is not initialized.")
            row = session.execute(
                select(ScientificSourceTaskAllocationRow).where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id == task_id,
                )
            ).scalar_one()
            if row.status == "prerequisite_blocked":
                return self._decision(row)
            if row.status not in {"pending", "running"}:
                raise ValueError("Only pending fair-scheduled tasks can be prepared.")
            expandable = max(row.maximum_requests - row.allocated_requests, 0)
            task_rows = list(
                session.execute(
                    select(ScientificSourceTaskAllocationRow).where(
                        ScientificSourceTaskAllocationRow.policy_id == policy_id,
                        ScientificSourceTaskAllocationRow.status != "terminal",
                    )
                ).scalars()
            )
            bucket_headroom: list[int] = []
            for bucket in session.execute(
                select(ScientificSourceAllocationBucketRow).where(
                    ScientificSourceAllocationBucketRow.policy_id == policy_id
                )
            ).scalars():
                applies = (
                    (bucket.bucket_kind == "provider" and row.provider == bucket.bucket_key)
                    or (
                        bucket.bucket_kind == "source_family"
                        and row.source_family == bucket.bucket_key
                    )
                    or (
                        bucket.bucket_kind == "evidence_role"
                        and row.evidence_role == bucket.bucket_key
                    )
                )
                if not applies:
                    continue
                allocated = sum(
                    task.allocated_requests
                    for task in task_rows
                    if (
                        (bucket.bucket_kind == "provider" and task.provider == bucket.bucket_key)
                        or (
                            bucket.bucket_kind == "source_family"
                            and task.source_family == bucket.bucket_key
                        )
                        or (
                            bucket.bucket_kind == "evidence_role"
                            and task.evidence_role == bucket.bucket_key
                        )
                    )
                )
                bucket_headroom.append(max(bucket.maximum_requests - allocated, 0))
            granted = min(
                expandable,
                policy.available_shared_remainder,
                *(bucket_headroom or [expandable]),
            )
            row.allocated_requests += granted
            row.status = "running"
            row.updated_at = now
            policy.available_shared_remainder -= granted
            policy.updated_at = now
            return self._decision(row)

    def acquire_if_configured(self, workflow_id: str, discovery_round: int, task_id: str) -> bool:
        """Consume one task/provider/role allocation; return False when no policy exists."""

        policy_id = self._policy_id(workflow_id, discovery_round)
        now = utc_text()
        blocked_reason: str | None = None
        with self.database.session() as session:
            policy = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            if policy is None:
                return False
            task = session.execute(
                select(ScientificSourceTaskAllocationRow).where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id == task_id,
                )
            ).scalar_one_or_none()
            if task is None:
                return False
            if task.status == "prerequisite_blocked":
                blocked_reason = "provider_prerequisite_unmet"
            elif task.consumed_requests >= task.allocated_requests:
                blocked_reason = "task_request_allocation_exhausted"
            bucket_keys = [
                ("provider", task.provider),
                ("source_family", task.source_family),
                ("evidence_role", task.evidence_role),
            ]
            buckets = list(
                session.execute(
                    select(ScientificSourceAllocationBucketRow).where(
                        ScientificSourceAllocationBucketRow.policy_id == policy_id,
                    )
                ).scalars()
            )
            applicable = {
                (row.bucket_kind, row.bucket_key): row
                for row in buckets
                if (row.bucket_kind, row.bucket_key) in bucket_keys
            }
            for key in bucket_keys:
                bucket = applicable.get(key)
                if bucket is not None and bucket.consumed_requests >= bucket.maximum_requests:
                    blocked_reason = f"{key[0]}_request_allocation_exhausted"
                    break
            if blocked_reason is None:
                task.consumed_requests += 1
                task.updated_at = now
                for bucket in applicable.values():
                    bucket.consumed_requests += 1
                    bucket.updated_at = now
                return True
            task.blocked_requests += 1
            task.updated_at = now
        raise ScientificSourceTaskAllocationExhausted(
            f"Scientific-source request blocked by fair scheduler: {blocked_reason}."
        )

    def release_consumption(self, workflow_id: str, discovery_round: int, task_id: str) -> None:
        """Undo a scheduler token when the global governor rejects the same request."""

        policy_id = self._policy_id(workflow_id, discovery_round)
        now = utc_text()
        with self.database.session() as session:
            task = session.execute(
                select(ScientificSourceTaskAllocationRow).where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id == task_id,
                )
            ).scalar_one_or_none()
            if task is None or task.consumed_requests <= 0:
                return
            task.consumed_requests -= 1
            task.updated_at = now
            for bucket in session.execute(
                select(ScientificSourceAllocationBucketRow).where(
                    ScientificSourceAllocationBucketRow.policy_id == policy_id,
                    (
                        (ScientificSourceAllocationBucketRow.bucket_kind == "provider")
                        & (ScientificSourceAllocationBucketRow.bucket_key == task.provider)
                    )
                    | (
                        (ScientificSourceAllocationBucketRow.bucket_kind == "source_family")
                        & (ScientificSourceAllocationBucketRow.bucket_key == task.source_family)
                    )
                    | (
                        (ScientificSourceAllocationBucketRow.bucket_kind == "evidence_role")
                        & (ScientificSourceAllocationBucketRow.bucket_key == task.evidence_role)
                    ),
                )
            ).scalars():
                bucket.consumed_requests = max(bucket.consumed_requests - 1, 0)
                bucket.updated_at = now

    def mark_terminal(
        self,
        workflow_id: str,
        discovery_round: int,
        task_id: str,
        *,
        outcome: str,
        completion_reason: str | None,
    ) -> None:
        policy_id = self._policy_id(workflow_id, discovery_round)
        now = utc_text()
        with self.database.session() as session:
            policy = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            task = session.execute(
                select(ScientificSourceTaskAllocationRow).where(
                    ScientificSourceTaskAllocationRow.policy_id == policy_id,
                    ScientificSourceTaskAllocationRow.task_id == task_id,
                )
            ).scalar_one_or_none()
            if policy is None or task is None or task.status == "terminal":
                return
            unused = max(task.allocated_requests - task.consumed_requests, 0)
            task.released_requests += unused
            task.status = "terminal"
            task.terminal_outcome = outcome[:120]
            task.completion_reason = completion_reason
            task.updated_at = now
            policy.available_shared_remainder += unused
            policy.updated_at = now

    def reconcile(self, workflow_id: str, discovery_round: int) -> dict[str, Any] | None:
        """Rebuild scheduler consumption from immutable global-governor attempts."""

        policy_id = self._policy_id(workflow_id, discovery_round)
        now = utc_text()
        with self.database.session() as session:
            policy = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            if policy is None:
                return None
            counts = {
                str(task_id): int(count)
                for task_id, count in session.execute(
                    select(
                        ScientificSourceRequestAttemptRow.task_id,
                        func.count(ScientificSourceRequestAttemptRow.id),
                    )
                    .where(
                        ScientificSourceRequestAttemptRow.workflow_id == workflow_id,
                        ScientificSourceRequestAttemptRow.discovery_round == discovery_round,
                        ScientificSourceRequestAttemptRow.reservation_sequence.is_not(None),
                    )
                    .group_by(ScientificSourceRequestAttemptRow.task_id)
                )
            }
            tasks = list(
                session.execute(
                    select(ScientificSourceTaskAllocationRow).where(
                        ScientificSourceTaskAllocationRow.policy_id == policy_id
                    )
                ).scalars()
            )
            for task in tasks:
                task.consumed_requests = counts.get(task.task_id, 0)
                task.updated_at = now
            for bucket in session.execute(
                select(ScientificSourceAllocationBucketRow).where(
                    ScientificSourceAllocationBucketRow.policy_id == policy_id
                )
            ).scalars():
                bucket.consumed_requests = sum(
                    task.consumed_requests
                    for task in tasks
                    if (
                        (bucket.bucket_kind == "provider" and task.provider == bucket.bucket_key)
                        or (
                            bucket.bucket_kind == "source_family"
                            and task.source_family == bucket.bucket_key
                        )
                        or (
                            bucket.bucket_kind == "evidence_role"
                            and task.evidence_role == bucket.bucket_key
                        )
                    )
                )
                bucket.updated_at = now
            policy.updated_at = now
        return self.state(workflow_id, discovery_round)

    def state(self, workflow_id: str, discovery_round: int) -> dict[str, Any] | None:
        policy_id = self._policy_id(workflow_id, discovery_round)
        with self.database.session() as session:
            policy = session.get(ScientificSourceAllocationPolicyRow, policy_id)
            if policy is None:
                return None
            tasks = list(
                session.execute(
                    select(ScientificSourceTaskAllocationRow)
                    .where(ScientificSourceTaskAllocationRow.policy_id == policy_id)
                    .order_by(
                        ScientificSourceTaskAllocationRow.sequence,
                        ScientificSourceTaskAllocationRow.task_id,
                    )
                ).scalars()
            )
            buckets = list(
                session.execute(
                    select(ScientificSourceAllocationBucketRow)
                    .where(ScientificSourceAllocationBucketRow.policy_id == policy_id)
                    .order_by(
                        ScientificSourceAllocationBucketRow.bucket_kind,
                        ScientificSourceAllocationBucketRow.bucket_key,
                    )
                ).scalars()
            )
            return {
                "policy_id": policy.id,
                "policy_version": policy.policy_version,
                "plan_fingerprint": policy.plan_fingerprint,
                "maximum_requests": policy.maximum_requests,
                "initial_shared_remainder": policy.initial_shared_remainder,
                "available_shared_remainder": policy.available_shared_remainder,
                "tasks": [
                    {
                        **self._decision(row).__dict__,
                        "remaining_requests": max(
                            row.allocated_requests - row.consumed_requests, 0
                        ),
                    }
                    for row in tasks
                ],
                "buckets": [
                    {
                        "kind": row.bucket_kind,
                        "key": row.bucket_key,
                        "maximum_requests": row.maximum_requests,
                        "consumed_requests": row.consumed_requests,
                    }
                    for row in buckets
                ],
            }
