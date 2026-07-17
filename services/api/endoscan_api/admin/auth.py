"""Explicit Phase-0 development authorization; production authentication is deferred."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request

ADMIN_MARKER = "local-development"
SAFE_ID = re.compile(r"^(?:build|approval|art|run|step|err)-[0-9a-f-]{36}$")


class AdminMutationLimiter:
    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        entries = self._entries[key]
        while entries and entries[0] <= now - self.window_seconds:
            entries.popleft()
        if len(entries) >= self.limit:
            raise HTTPException(status_code=429, detail="Admin mutation rate limit exceeded.")
        entries.append(now)


def require_development_admin(
    request: Request,
    x_endoscan_admin: str | None = Header(default=None, alias="X-EndoScan-Admin"),
) -> str:
    if not bool(getattr(request.app.state, "admin_development_mode", False)):
        raise HTTPException(
            status_code=403,
            detail="Admin routes are disabled outside explicitly configured development mode.",
        )
    if x_endoscan_admin != ADMIN_MARKER:
        raise HTTPException(
            status_code=401,
            detail="Explicit local-development admin marker is required.",
        )
    return "local-admin"


def require_mutation_budget(request: Request) -> None:
    limiter: AdminMutationLimiter = request.app.state.admin_mutation_limiter
    client = request.client.host if request.client else "unknown"
    limiter.check(client)


def validate_resource_id(value: str, label: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise HTTPException(status_code=404, detail=f"{label} was not found.")
    return value
