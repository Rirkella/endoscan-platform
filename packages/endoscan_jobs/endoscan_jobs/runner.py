"""Job dispatch, structured logging, and the exit-code contract.

Exit codes (mirroring the M5 agent's "failed_qc is a valid outcome, not a failure"):
- ``0`` the job RAN and wrote its report — INCLUDING a ``failed_qc`` coverage verdict
  (a valid data outcome with a report, not a runner error);
- ``2`` job error (fetch/parse failed, no data, unexpected exception);
- ``3`` usage error (unknown job / bad CLI args) — raised in the CLI layer;
- ``4`` environment error (storage root unwritable, missing dependency).

A job is a callable ``(JobContext) -> JobOutcome``; it RAISES :class:`JobError` for a
data/runtime failure and returns normally (with a verdict in its report) otherwise.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from .storage import Storage

EXIT_OK = 0
EXIT_JOB_ERROR = 2
EXIT_USAGE = 3
EXIT_ENV_ERROR = 4


class JobError(RuntimeError):
    """A data/runtime failure inside a job -> exit 2 (NOT used for a failed_qc verdict)."""


class JobOutcome(BaseModel):
    """What a job returns on success (it ran; the verdict may still be failed_qc)."""

    model_config = ConfigDict(extra="forbid")

    report_key: str | None = None
    summary: str = ""
    verdict: str | None = None  # e.g. "PASS" | "failed_qc" for coverage


class JobResult(BaseModel):
    """The machine-readable record Claude Code reads to triage a run."""

    model_config = ConfigDict(extra="forbid")

    job: str
    target: str
    run_id: str
    status: str  # "ok" | "error"
    exit_code: int
    started_at: str
    ended_at: str
    report_key: str | None = None
    log_key: str | None = None
    verdict: str | None = None
    summary: str = ""
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def generate_run_id(job: str, target: str) -> str:
    return f"{job}-{target}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"


class JobContext:
    """Per-run state handed to a job: target, options, storage, and a structured logger."""

    def __init__(self, job: str, target: str, run_id: str, storage: Storage, options: dict) -> None:
        self.job = job
        self.target = target
        self.run_id = run_id
        self.storage = storage
        self.options = options
        self.events: list[dict] = []
        self.log_key = f"logs/{run_id}/events.jsonl"

    def log(self, event: str, **detail: object) -> None:
        record = {"ts": _now_iso(), "event": event, **detail}
        self.events.append(record)
        print(f"[{self.run_id}] {event} {detail}")  # also to stdout for the CI/runner log

    def flush_logs(self) -> None:
        text = "\n".join(json.dumps(e, default=str) for e in self.events) + "\n"
        self.storage.put_text(self.log_key, text)


# Job registry — populated at import time from the jobs subpackage.
def _load_jobs() -> dict[str, Callable[[JobContext], JobOutcome]]:
    from .jobs.build_explore_map import build_explore_map_job
    from .jobs.coverage import coverage_job
    from .jobs.extract_signatures import extract_signatures_job

    return {
        "coverage": coverage_job,
        "extract_signatures": extract_signatures_job,
        "build_explore_map": build_explore_map_job,
    }


JOBS: dict[str, Callable[[JobContext], JobOutcome]] = _load_jobs()


def run_job(
    job: str,
    target: str,
    *,
    storage: Storage,
    run_id: str | None = None,
    options: dict | None = None,
) -> JobResult:
    """Run ``job`` for ``target``, write logs + result.json via ``storage``, return result.

    Never raises for a data failure — it captures the error into the JobResult and sets
    ``exit_code`` so the caller (CLI) can ``sys.exit`` deterministically.
    """
    run_id = run_id or generate_run_id(job, target)
    started = _now_iso()
    ctx = JobContext(job, target, run_id, storage, options or {})
    report_key: str | None = None
    verdict: str | None = None
    summary = ""
    error: str | None = None

    if job not in JOBS:
        status, exit_code, error = (
            "error",
            EXIT_USAGE,
            f"unknown job {job!r}; known: {sorted(JOBS)}",
        )
    else:
        try:
            ctx.log("start", job=job, target=target)
            outcome = JOBS[job](ctx)
            status, exit_code = "ok", EXIT_OK
            report_key, summary, verdict = outcome.report_key, outcome.summary, outcome.verdict
            ctx.log("done", verdict=verdict, report_key=report_key)
        except JobError as exc:
            status, exit_code, error = "error", EXIT_JOB_ERROR, str(exc)
            ctx.log("job_error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - any uncaught error is a job error (exit 2)
            status, exit_code, error = "error", EXIT_JOB_ERROR, repr(exc)
            ctx.log("unexpected_error", error=repr(exc), traceback=traceback.format_exc())

    try:
        ctx.flush_logs()
    except Exception:  # noqa: BLE001 - logging must never mask the job result
        pass

    result = JobResult(
        job=job,
        target=target,
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        started_at=started,
        ended_at=_now_iso(),
        report_key=report_key,
        log_key=ctx.log_key,
        verdict=verdict,
        summary=summary,
        error=error,
    )
    try:
        storage.put_text(f"logs/{run_id}/result.json", result.model_dump_json(indent=2))
    except Exception:  # noqa: BLE001 - persisting the result file is best-effort
        pass
    return result
