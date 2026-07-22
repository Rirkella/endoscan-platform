"""Liveness + which committed endpoints are warmed and whether /explain is available."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/", response_model=HealthResponse)
@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    return HealthResponse(
        status="ok",
        endpoints_loaded=list(state.endpoints_loaded),
        explain_available=bool(state.explain_available),
        explanation_capabilities=dict(state.explanation_capabilities),
        pubmed=dict(state.pubmed_capability),
        workflow_database=dict(state.workflow_database.capability()),
        artifact_store=dict(state.artifact_store.capability()),
        agent_provider={
            "configured": list(state.provider_registry.configured()),
            "live_model_api": bool(state.agent_configuration.live_enabled),
            "label": f"{state.agent_configuration.run_mode.value.title()} agent mode",
            "model": state.agent_configuration.model,
            "api_key_present": state.agent_configuration.api_key_present,
        },
        admin={
            "development_mode": bool(state.admin_development_mode),
            "production_authorization_implemented": False,
        },
    )
