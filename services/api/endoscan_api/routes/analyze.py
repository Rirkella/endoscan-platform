"""POST /analyze — run one signature across ALL registered endpoints in a single call.

Thin: loops the tested ``predict`` over ``list_endpoints()`` — NO new science. Per-endpoint
isolation: one endpoint erroring yields ``ok:false`` + its structured error while the others
still return ``ok:true``; the top-level response is 200 with the full per-endpoint array (the
shape the score-card grid consumes). A malformed request body is 422 (pydantic).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from endoscan_core.inference import (
    ModelArtifactUnavailableError,
    PredictionResult,
    SignatureValidationError,
    predict,
)
from endoscan_core.registry import EndpointNotFoundError, list_endpoints

from ..deps import get_repo_root
from ..schemas import AnalyzeEndpointResult, AnalyzeRequest, AnalyzeResponse, ErrorResponse

router = APIRouter(tags=["analyze"])

# Per-endpoint failures we isolate into an error entry (never sink the whole fan-out).
_ISOLATED = (SignatureValidationError, ModelArtifactUnavailableError, EndpointNotFoundError)
_ERROR_CODE = {
    SignatureValidationError: "invalid_signature",
    ModelArtifactUnavailableError: "model_unavailable",
    EndpointNotFoundError: "endpoint_not_found",
}


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(body: AnalyzeRequest, repo_root: Path = Depends(get_repo_root)) -> AnalyzeResponse:
    results: list[AnalyzeEndpointResult] = []
    for entry in list_endpoints(repo_root=repo_root):
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
                        detail=str(exc),
                        endpoint_id=entry.endpoint_id,
                    ),
                )
            )
    return AnalyzeResponse(results=results)
