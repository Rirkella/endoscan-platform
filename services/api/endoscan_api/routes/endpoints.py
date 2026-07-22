"""Read-only endpoint listing + detail — thin wrappers over the registry store."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from endoscan_core.registry import get_endpoint, list_endpoints

from ..assemblers import build_endpoint_detail
from ..deps import get_repo_root
from ..schemas import EndpointDetail, EndpointSummary, ExplanationCapabilityStatus

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


def _status_str(entry) -> str:
    return entry.status.value if hasattr(entry.status, "value") else str(entry.status)


def _explanation_status(request: Request, entry) -> dict:
    return request.app.state.explanation_capabilities.get(
        entry.endpoint_id,
        {
            "declared_method": entry.explanation.method if entry.explanation else None,
            "available": False,
            "missing_dependencies": [],
            "reason": "The endpoint was published after process startup; reload warms its model.",
        },
    )


@router.get("", response_model=list[EndpointSummary])
def list_all(request: Request, repo_root: Path = Depends(get_repo_root)) -> list[EndpointSummary]:
    """All registered endpoints (ER, AR) with their honest status surfaced."""
    return [
        EndpointSummary(
            endpoint_id=e.endpoint_id,
            biological_target=e.biological_target,
            status=_status_str(e),
            input_type=e.input_type,
            frozen=e.frozen,
            explanation=ExplanationCapabilityStatus.model_validate(_explanation_status(request, e)),
            validation_status=e.validation_status.model_dump(mode="json"),
        )
        for e in list_endpoints(repo_root=repo_root)
    ]


@router.get("/{endpoint_id}", response_model=EndpointDetail)
def detail(
    endpoint_id: str, request: Request, repo_root: Path = Depends(get_repo_root)
) -> EndpointDetail:
    """Full detail: the variant-forward-compatible shape + limitations + metrics summary.

    ``get_endpoint`` raises ``EndpointNotFoundError`` for an unknown id -> 404 (handler).
    """
    entry = get_endpoint(endpoint_id, repo_root=repo_root)
    return build_endpoint_detail(entry, repo_root, _explanation_status(request, entry))
