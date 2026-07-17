"""HTTP routers exposed by the thin serving API."""

from __future__ import annotations

from . import analyze, catalogue, endpoints, explore, health, inference, interpret, signatures

__all__ = [
    "analyze",
    "catalogue",
    "endpoints",
    "explore",
    "health",
    "inference",
    "interpret",
    "signatures",
]
