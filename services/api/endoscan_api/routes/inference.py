"""Predict + explain — thin wrappers over the tested core inference functions.

Zero science here: the handlers pass the request signature straight to
``endoscan_core.inference.predict`` / ``explain`` and return their results verbatim
(each already carries the required ``LimitationsBlock``). Model binaries are the
COMMITTED artifacts (no DVC pull).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from endoscan_core.inference import (
    ExplanationResult,
    PredictionResult,
    UnsupportedModelForExplanationError,
    explain,
    predict,
)

from ..deps import get_repo_root
from ..schemas import ErrorResponse, ExplainRequest, PredictRequest

router = APIRouter(tags=["inference"])


@router.post("/predict", response_model=PredictionResult)
def post_predict(
    body: PredictRequest, repo_root: Path = Depends(get_repo_root)
) -> PredictionResult:
    """Score one signature. ``predict`` returns a single result for a dict input.

    Raises (handled): ``EndpointNotFoundError`` -> 404, ``SignatureValidationError`` -> 422,
    ``ModelArtifactUnavailableError`` -> 500. A malformed body is 422 (pydantic).
    """
    result = predict(
        body.endpoint_id,
        body.signature,
        repo_root=repo_root,
        allow_extra=body.allow_extra,
    )
    # A dict signature always yields a single PredictionResult (never a list).
    return result if isinstance(result, PredictionResult) else result[0]


@router.post("/explain", response_model=ExplanationResult)
def post_explain(body: ExplainRequest, repo_root: Path = Depends(get_repo_root)):
    """Top-N signed gene contributors + limitations; attributor auto-selected by model type.

    Core ``explain`` picks the method: tree models -> TreeSHAP (``method="tree_shap"``),
    linear ``coef_`` models -> coefficient attribution (``method="linear_coefficient"``). So
    ER (random_forest) and AR (ridge_logreg) both return 200 with the honest LimitationsBlock.
    Clean error paths (never a bare 500):
    - a model type neither tree nor linear -> ``UnsupportedModelForExplanationError`` -> 501;
    - TreeSHAP needs the optional ``shap`` extra; if absent for a tree endpoint -> 503.
    ``SignatureValidationError``/``EndpointNotFoundError`` propagate to the 422/404 handlers.
    """
    try:
        result = explain(
            body.endpoint_id,
            body.signature,
            top_n=body.top_n,
            repo_root=repo_root,
            allow_extra=body.allow_extra,
        )
    except UnsupportedModelForExplanationError as exc:
        return JSONResponse(
            status_code=501,
            content=ErrorResponse(
                error="explain_unsupported_for_model",
                detail=f"{exc}. Predictions via /predict are unaffected.",
                endpoint_id=body.endpoint_id,
            ).model_dump(),
        )
    except ImportError as exc:  # shap (the `explain` extra) not installed, tree endpoint
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error="explain_unavailable",
                detail=f"TreeSHAP explainability requires the 'explain' extra (shap): {exc}",
                endpoint_id=body.endpoint_id,
            ).model_dump(),
        )
    return result if isinstance(result, ExplanationResult) else result[0]
