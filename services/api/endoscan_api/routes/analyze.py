"""POST /analyze — run one signature across selected or all registered endpoints.

Thin: loops the tested ``predict`` over ``list_endpoints()`` — NO new science. Per-endpoint
isolation: one endpoint erroring yields ``ok:false`` + its structured error while the others
still return ``ok:true``; the top-level response is 200 with the full per-endpoint array (the
shape the score-card grid consumes). A malformed request body is 422 (pydantic).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from endoscan_core.inference import (
    ModelArtifactUnavailableError,
    PredictionResult,
    SignatureValidationError,
    predict,
)
from endoscan_core.registry import EndpointNotFoundError, get_endpoint, list_endpoints

from ..deps import get_repo_root
from ..schemas import (
    AnalyzeEndpointResult,
    AnalyzeRequest,
    AnalyzeResponse,
    AnalyzeSummary,
    ErrorResponse,
)

router = APIRouter(tags=["analyze"])

# Per-endpoint failures we isolate into an error entry (never sink the whole fan-out).
_ISOLATED = (SignatureValidationError, ModelArtifactUnavailableError, EndpointNotFoundError)
_ERROR_CODE = {
    SignatureValidationError: "invalid_signature",
    ModelArtifactUnavailableError: "model_unavailable",
    EndpointNotFoundError: "endpoint_not_found",
}


def _public_failure_detail(exc: Exception) -> str:
    if isinstance(exc, SignatureValidationError):
        return str(exc)
    if isinstance(exc, ModelArtifactUnavailableError):
        return "The registered model is temporarily unavailable."
    return "The requested endpoint is not available."


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(
    body: AnalyzeRequest,
    request: Request,
    repo_root: Path = Depends(get_repo_root),
) -> AnalyzeResponse:
    results: list[AnalyzeEndpointResult] = []
    entries = (
        [get_endpoint(endpoint_id, repo_root=repo_root) for endpoint_id in body.endpoint_ids]
        if body.endpoint_ids is not None
        else list_endpoints(repo_root=repo_root)
    )
    for entry in entries:
        try:
            out = predict(
                entry.endpoint_id, body.signature, repo_root=repo_root, allow_extra=body.allow_extra
            )
            result = out if isinstance(out, PredictionResult) else out[0]
            results.append(
                AnalyzeEndpointResult(
                    endpoint_id=entry.endpoint_id,
                    biological_target=entry.biological_target,
                    ok=True,
                    result=result,
                )
            )
        except _ISOLATED as exc:
            results.append(
                AnalyzeEndpointResult(
                    endpoint_id=entry.endpoint_id,
                    biological_target=entry.biological_target,
                    ok=False,
                    error=ErrorResponse(
                        error=_ERROR_CODE[type(exc)],
                        detail=_public_failure_detail(exc),
                        endpoint_id=entry.endpoint_id,
                        request_id=request.state.request_id,
                    ),
                )
            )
    succeeded = sum(item.ok for item in results)
    failed = len(results) - succeeded
    status = "ok" if failed == 0 else "all_failed" if succeeded == 0 else "partial"
    return AnalyzeResponse(
        results=results,
        summary=AnalyzeSummary(
            requested=len(results), succeeded=succeeded, failed=failed, status=status
        ),
    )
