"""Shared dependencies: repo-root resolution + a cached model loader.

The repo root is resolved ONCE (at app creation) and stored on ``app.state``; routes
read it via :func:`get_repo_root`. Model binaries are loaded through
:func:`get_cached_model`, a process-lifetime cache over the tested
``endoscan_core.inference.load_endpoint_model`` — which loads the COMMITTED
``model.pkl`` (no DVC pull, no remote/credentials) and raises
``ModelArtifactUnavailableError`` if a binary is somehow absent.

Note (thin-by-design tradeoff): ``/predict`` and ``/explain`` call the tested core
``predict``/``explain`` orchestration, which loads the model internally per call — we do
NOT reimplement that glue here just to inject a cached model (that would duplicate core
orchestration). :func:`get_cached_model` is used for startup warm-up and ``/health``
readiness reporting. Threading a preloaded model into core inference would be a core
change, out of M7 scope.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from fastapi import Request

from endoscan_core.inference import load_endpoint_model


def get_repo_root(request: Request) -> Path:
    """The repository root resolved at app startup (anchors all artifact reads)."""
    return request.app.state.repo_root


@cache
def get_cached_model(endpoint_id: str, repo_root: Path) -> Any:
    """Load (and cache) a registered endpoint's COMMITTED model binary.

    Thin pass-through to ``load_endpoint_model``; cached so startup warm-up and
    ``/health`` do not re-deserialize the pickle. ``repo_root`` is part of the key so a
    differently-rooted app (e.g. a test) never reuses another root's cache.
    """
    return load_endpoint_model(endpoint_id, repo_root=repo_root)
