"""FastAPI application factory for the EndoScan thin serving API.

``create_app`` resolves the repo root ONCE (anchoring all artifact reads), warms the
committed model binaries (no DVC pull, no credentials), records which endpoints loaded
and whether ``/explain`` is available (shap present), then wires the read-only routers
and the core-exception -> HTTP handlers.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from endoscan_core.inference import ModelArtifactUnavailableError
from endoscan_core.registry import find_repo_root, list_endpoints

from .deps import get_cached_model
from .errors import register_exception_handlers
from .routes import analyze, endpoints, explore, health, inference, interpret, signatures

API_TITLE = "EndoScan Serving API"
API_DESCRIPTION = (
    "Read-only serving over the EndoScan registry + endoscan_core inference. "
    "Serves the committed ER/AR models. Predictions are EXPERIMENTAL pre-screening "
    "hypotheses, not regulatory/clinical/diagnostic outputs — see each result's limitations."
)

#: Env var holding a comma-separated EXPLICIT allow-list of frontend origins (deployment).
CORS_ORIGINS_ENV = "ENDOSCAN_CORS_ORIGINS"
#: Default when the env var is unset: the local Vite dev origins (5173 default, plus 5174/5175
#: fallback ports). NEVER a wildcard — the safe, explicit list is the default.
DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
    "http://127.0.0.1:5175",
    "http://localhost:5175",
]


def _cors_origins() -> list[str]:
    """Allowed CORS origins: exactly ``ENDOSCAN_CORS_ORIGINS`` when set, else the localhost default.

    Parsed by splitting on commas, stripping whitespace, and dropping empties. There is NO wildcard
    fallback — a deployer who truly wants ``*`` must set it explicitly in the env var (not
    recommended for a deployed API); the default and recommended path are explicit origins.
    """
    raw = os.environ.get(CORS_ORIGINS_ENV)
    if raw is not None:
        parsed = [o.strip() for o in raw.split(",") if o.strip()]
        if parsed:
            return parsed
    return list(DEFAULT_CORS_ORIGINS)


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
    app.state.cors_origins = _cors_origins()

    # CORS: explicit allow-list only (browser cross-origin fetch fix for the local demo /
    # deployment). Minimal surface — the GET/POST routes + OPTIONS preflight, Content-Type only,
    # no credentials (keeps the surface minimal and sidesteps the credentials+wildcard CORS rule).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app.state.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(endpoints.router)
    app.include_router(inference.router)
    app.include_router(signatures.router)
    app.include_router(analyze.router)
    app.include_router(explore.router)
    app.include_router(interpret.router)
    return app
