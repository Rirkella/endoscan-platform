"""HTTP routers: health, endpoints (list/detail), inference (predict/explain)."""

from __future__ import annotations

from . import endpoints, health, inference

__all__ = ["endpoints", "health", "inference"]
