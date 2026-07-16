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
from .middleware import RequestContextMiddleware
from .routes import analyze, catalogue, endpoints, explore, health, inference, interpret, signatures

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


def _estimator(model):
    return model.steps[-1][1] if hasattr(model, "steps") and model.steps else model


def _capability(entry, model) -> dict:
    declared = entry.explanation
    if declared is None:
        return {
            "declared_method": None,
            "available": False,
            "missing_dependencies": [],
            "reason": "No explanation capability is declared for this endpoint.",
        }
    missing = [
        name
        for name in declared.required_dependencies
        if importlib.util.find_spec(name) is None
    ]
    estimator = _estimator(model)
    method_supported = (
        hasattr(estimator, "feature_importances_")
        if declared.method == "tree_shap"
        else hasattr(estimator, "coef_")
    )
    reason = None
    if missing:
        reason = f"Missing runtime dependencies: {', '.join(missing)}."
    elif not method_supported:
        reason = f"The loaded model does not support declared method {declared.method}."
    return {
        "declared_method": declared.method,
        "available": not missing and method_supported,
        "missing_dependencies": missing,
        "reason": reason,
    }


def _warm_models(repo_root: Path) -> tuple[list[str], dict[str, dict]]:
    """Load models and validate each declared explanation capability at startup."""
    loaded: list[str] = []
    capabilities: dict[str, dict] = {}
    for entry in list_endpoints(repo_root=repo_root):
        try:
            model = get_cached_model(entry.endpoint_id, repo_root)
            loaded.append(entry.endpoint_id)
            capabilities[entry.endpoint_id] = _capability(entry, model)
        except ModelArtifactUnavailableError:
            capabilities[entry.endpoint_id] = {
                "declared_method": entry.explanation.method if entry.explanation else None,
                "available": False,
                "missing_dependencies": [],
                "reason": "The registered model artifact could not be loaded.",
            }
            continue
    return loaded, capabilities


def create_app(repo_root: Path | None = None) -> FastAPI:
    """Build the app. ``repo_root`` defaults to the resolved workspace root."""
    root = repo_root or find_repo_root()

    app = FastAPI(title=API_TITLE, description=API_DESCRIPTION, version="0.0.0")
    app.state.repo_root = root
    loaded, capabilities = _warm_models(root)
    app.state.explanation_capabilities = capabilities
    app.state.explain_available = any(item["available"] for item in capabilities.values())
    app.state.endpoints_loaded = loaded
    app.state.cors_origins = _cors_origins()
    ncbi_email = (os.environ.get("NCBI_EMAIL") or "").strip()
    app.state.pubmed_capability = {
        "configured": bool(ncbi_email),
        "available": bool(ncbi_email),
        "reason": (
            None
            if ncbi_email
            else "NCBI_EMAIL is not configured; PubMed integration is disabled."
        ),
    }

    # CORS: explicit allow-list only (browser cross-origin fetch fix for the local demo /
    # deployment). Minimal surface — the GET/POST routes + OPTIONS preflight, Content-Type only,
    # no credentials (keeps the surface minimal and sidesteps the credentials+wildcard CORS rule).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app.state.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(endpoints.router)
    app.include_router(inference.router)
    app.include_router(signatures.router)
    app.include_router(analyze.router)
    app.include_router(catalogue.router)
    app.include_router(explore.router)
    app.include_router(interpret.router)
    return app
