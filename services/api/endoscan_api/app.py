"""FastAPI application factory for the EndoScan thin serving API.

``create_app`` resolves the repo root ONCE (anchoring all artifact reads), warms the
committed model binaries (no DVC pull, no credentials), records which endpoints loaded
and whether ``/explain`` is available (shap present), then wires the read-only routers
and the core-exception -> HTTP handlers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import FastAPI

from endoscan_core.inference import ModelArtifactUnavailableError
from endoscan_core.registry import find_repo_root, list_endpoints

from .deps import get_cached_model
from .errors import register_exception_handlers
from .routes import endpoints, health, inference

API_TITLE = "EndoScan Serving API"
API_DESCRIPTION = (
    "Read-only serving over the EndoScan registry + endoscan_core inference. "
    "Serves the committed ER/AR models. Predictions are EXPERIMENTAL pre-screening "
    "hypotheses, not regulatory/clinical/diagnostic outputs — see each result's limitations."
)


def _warm_models(repo_root: Path) -> list[str]:
    """Load each registered endpoint's committed binary; return the ids that loaded."""
    loaded: list[str] = []
    for entry in list_endpoints(repo_root=repo_root):
        try:
            get_cached_model(entry.endpoint_id, repo_root)
            loaded.append(entry.endpoint_id)
        except ModelArtifactUnavailableError:
            # Defensive: binaries are committed, so this is not expected. Skip, don't crash.
            continue
    return loaded


def create_app(repo_root: Path | None = None) -> FastAPI:
    """Build the app. ``repo_root`` defaults to the resolved workspace root."""
    root = repo_root or find_repo_root()

    app = FastAPI(title=API_TITLE, description=API_DESCRIPTION, version="0.0.0")
    app.state.repo_root = root
    app.state.explain_available = importlib.util.find_spec("shap") is not None
    app.state.endpoints_loaded = _warm_models(root)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(endpoints.router)
    app.include_router(inference.router)
    return app
