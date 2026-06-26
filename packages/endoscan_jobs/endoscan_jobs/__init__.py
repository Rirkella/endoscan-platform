"""EndoScan job runner: a containerized entrypoint that runs named jobs and writes
structured logs + artifacts through a backend-agnostic Storage interface.

The future read-only M7 API consumes the SAME registry + Storage the runner writes, so
no rework is needed: M7 reads what the jobs write.
"""

from __future__ import annotations

from .runner import JobResult, run_job
from .storage import LocalStorage, Storage, StorageKeyError

__all__ = ["JobResult", "LocalStorage", "Storage", "StorageKeyError", "run_job"]
