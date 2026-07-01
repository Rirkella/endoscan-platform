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

from endoscan_core.inference import ExplanationResult, PredictionResult, explain, predict

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


def _shap_invalid_model_error() -> type[Exception] | None:
    """shap's 'model type not supported by TreeExplainer' error class, if shap is present."""
    try:
        from shap.utils._exceptions import InvalidModelError

        return InvalidModelError
    except Exception:  # shap absent or internal path moved
        return None


@router.post("/explain", response_model=ExplanationResult)
def post_explain(body: ExplainRequest, repo_root: Path = Depends(get_repo_root)):
    """Top-N signed gene contributors + limitations, via TreeSHAP.

    Two availability caveats, both returned as clean errors (never a bare 500):
    - ``shap`` is an OPTIONAL dependency imported lazily inside core; if it is not
      installed, return 503.
    - The only implemented attributor is TreeSHAP, so ``/explain`` supports TREE-based
      endpoints (e.g. ER=random_forest). A linear endpoint (e.g. AR=ridge_logreg) raises
      shap's ``InvalidModelError`` -> return 501. Coefficient attribution for linear models
      is future core work (the ``AttributionMethod`` protocol is designed for it), NOT M7.
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
    except ImportError as exc:  # shap (the `explain` extra) not installed
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error="explain_unavailable",
                detail=f"explainability requires the 'explain' extra (shap): {exc}",
                endpoint_id=body.endpoint_id,
            ).model_dump(),
        )
    except Exception as exc:  # narrow to shap's unsupported-model error; re-raise anything else
        invalid_model = _shap_invalid_model_error()
        if invalid_model is not None and isinstance(exc, invalid_model):
            return JSONResponse(
                status_code=501,
                content=ErrorResponse(
                    error="explain_unsupported_for_model",
                    detail=(
                        "TreeSHAP supports tree-based endpoints only; this endpoint's model "
                        f"is not supported ({exc}). Predictions via /predict are unaffected."
                    ),
                    endpoint_id=body.endpoint_id,
                ).model_dump(),
            )
        raise
    return result if isinstance(result, ExplanationResult) else result[0]
