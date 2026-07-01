"""Map core exceptions to clean HTTP responses (thin translation, no logic).

Core exception MESSAGES are surfaced verbatim (e.g. the gene-level SignatureValidationError
text) — the API never re-derives which genes are missing/extra.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from endoscan_core.inference import (
    ModelArtifactUnavailableError,
    SignatureValidationError,
)
from endoscan_core.registry import EndpointNotFoundError

from .schemas import ErrorResponse


def _payload(error: str, detail: str, endpoint_id: str | None = None) -> dict:
    return ErrorResponse(error=error, detail=detail, endpoint_id=endpoint_id).model_dump()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(EndpointNotFoundError)
    async def _not_found(request: Request, exc: EndpointNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_payload("endpoint_not_found", str(exc)))

    @app.exception_handler(SignatureValidationError)
    async def _bad_signature(request: Request, exc: SignatureValidationError) -> JSONResponse:
        # Well-formed JSON but the signature failed the endpoint's schema contract.
        return JSONResponse(status_code=422, content=_payload("invalid_signature", str(exc)))

    @app.exception_handler(ModelArtifactUnavailableError)
    async def _model_unavailable(
        request: Request, exc: ModelArtifactUnavailableError
    ) -> JSONResponse:
        # Defensive: both ER/AR binaries are committed, so this should not occur in serving.
        return JSONResponse(status_code=500, content=_payload("model_unavailable", str(exc)))
