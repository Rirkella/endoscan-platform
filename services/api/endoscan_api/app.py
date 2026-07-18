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
from endoscan_workflows.artifacts import LocalArtifactStore
from endoscan_workflows.config import AgentConfiguration
from endoscan_workflows.database import WorkflowDatabase
from endoscan_workflows.discovery_tools import DiscoveryToolService
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.openai_provider import OpenAIAgentProvider
from endoscan_workflows.providers import FakeAgentProvider, ProviderRegistry
from endoscan_workflows.service import WorkflowService
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificSourceClient
from endoscan_workflows.state_machine import WorkflowGraph
from endoscan_workflows.tools import phase1_tool_registry

from .admin.auth import AdminMutationLimiter
from .admin.routes import router as admin_router
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


def _workflow_state_machine_path(data_root: Path) -> Path:
    """Resolve canonical workflow policy independently of synthetic data roots."""
    supplied = data_root / "docs" / "agents" / "workflow-state-machine.json"
    if supplied.is_file():
        return supplied
    repository = Path(__file__).resolve().parents[3]
    canonical = repository / "docs" / "agents" / "workflow-state-machine.json"
    if canonical.is_file():
        return canonical
    raise RuntimeError("The canonical workflow state-machine document is unavailable.")


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
        name for name in declared.required_dependencies if importlib.util.find_spec(name) is None
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
            None if ncbi_email else "NCBI_EMAIL is not configured; PubMed integration is disabled."
        ),
    }
    workflow_db_path = Path(
        os.environ.get("ENDOSCAN_WORKFLOW_DB", str(root / ".endoscan" / "workflows.db"))
    )
    artifact_root = Path(
        os.environ.get("ENDOSCAN_ARTIFACT_ROOT", str(root / ".endoscan" / "artifacts"))
    )
    workflow_database = WorkflowDatabase(workflow_db_path)
    workflow_database.migrate()
    artifact_store = LocalArtifactStore(workflow_database, artifact_root)
    agent_configuration = AgentConfiguration.from_env()
    source_cache = SourceResponseCache(
        workflow_database, ttl_seconds=agent_configuration.source_cache_ttl_seconds
    )
    source_client = ScientificSourceClient(
        timeout_seconds=agent_configuration.source_request_timeout_seconds,
        maximum_bytes=agent_configuration.source_response_maximum_bytes,
        requests_per_second=agent_configuration.source_requests_per_second,
    )
    discovery_tools = DiscoveryToolService(
        source_cache,
        artifact_store,
        source_client,
        ncbi_email=agent_configuration.ncbi_email,
        ncbi_api_key=(
            agent_configuration.ncbi_api_key.get_secret_value()
            if agent_configuration.ncbi_api_key
            else None
        ),
    )
    tool_registry = phase1_tool_registry(root, discovery_tools)
    provider_registry = ProviderRegistry()
    provider_registry.register("fake", FakeAgentProvider)
    provider_registry.register(
        "openai", lambda: OpenAIAgentProvider(agent_configuration, tool_registry)
    )
    harness = AgentHarness(workflow_database, provider_registry, tool_registry)
    workflow_service = WorkflowService(
        workflow_database,
        artifact_store,
        WorkflowGraph(_workflow_state_machine_path(root)),
        repo_root=root,
        harness=harness,
        agent_configuration=agent_configuration,
    )
    recovered = workflow_service.recover_interrupted()
    app.state.workflow_database = workflow_database
    app.state.artifact_store = artifact_store
    app.state.provider_registry = provider_registry
    app.state.agent_configuration = agent_configuration
    app.state.source_cache = source_cache
    app.state.source_client = source_client
    app.state.workflow_service = workflow_service
    app.state.admin_development_mode = (
        os.environ.get("ENDOSCAN_ADMIN_MODE", "disabled").strip().lower() == "development"
    )
    app.state.admin_mutation_limiter = AdminMutationLimiter()
    app.state.agent_capabilities = {
        **agent_configuration.public_status(),
        "available_modes": ["live", "cached", "replay"],
        "label": f"{agent_configuration.run_mode.value.title()} agent mode",
        "live_llm_calls": (
            agent_configuration.live_enabled
            and agent_configuration.run_mode.value in {"live", "cached"}
        ),
        "live_dataset_discovery": agent_configuration.run_mode.value == "live",
        "rag": False,
        "registry_publication": False,
        "recovered_interrupted_steps": recovered,
    }
    app.router.add_event_handler("shutdown", source_client.close)
    app.router.add_event_handler("shutdown", workflow_database.dispose)

    # CORS: explicit allow-list only (browser cross-origin fetch fix for the local demo /
    # deployment). Minimal surface — the GET/POST routes + OPTIONS preflight, Content-Type only,
    # no credentials (keeps the surface minimal and sidesteps the credentials+wildcard CORS rule).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app.state.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Request-ID",
            "X-EndoScan-Admin",
            "Idempotency-Key",
        ],
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
    app.include_router(admin_router)
    return app
