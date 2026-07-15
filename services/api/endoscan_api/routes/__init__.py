"""HTTP routers: health, endpoints (list/detail), inference (predict/explain), signatures
(upload parse/validate), analyze (fan-out across all endpoints)."""

from __future__ import annotations

from . import analyze, catalogue, endpoints, health, inference, signatures

__all__ = ["analyze", "catalogue", "endpoints", "health", "inference", "signatures"]
