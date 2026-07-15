"""Translate internal exceptions into safe public errors with request IDs."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from endoscan_core.inference import ModelArtifactUnavailableError, SignatureValidationError
from endoscan_core.registry import EndpointNotFoundError

from .explore_store import (
    ExploreArtifactCorruptError,
    ExploreArtifactUnavailableError,
    ExploreContextError,
)
from .parsing import MalformedUploadError
from .schemas import ErrorResponse

logger = logging.getLogger(__name__)


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def error_payload(
    request: Request, error: str, detail: str, endpoint_id: str | None = None
) -> dict:
    return ErrorResponse(
        error=error,
        detail=detail,
        endpoint_id=endpoint_id,
        request_id=request_id(request),
    ).model_dump()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(EndpointNotFoundError)
    async def _not_found(request: Request, exc: EndpointNotFoundError) -> JSONResponse:
        logger.info("unknown endpoint request_id=%s error=%s", request_id(request), exc)
        return JSONResponse(
            status_code=404,
            content=error_payload(request, "endpoint_not_found", "The endpoint is not registered."),
        )

    @app.exception_handler(SignatureValidationError)
    async def _bad_signature(request: Request, exc: SignatureValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(request, "invalid_signature", str(exc)),
        )

    @app.exception_handler(MalformedUploadError)
    async def _malformed_upload(request: Request, exc: MalformedUploadError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload(request, "malformed_upload", str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.info("request validation failed request_id=%s errors=%s", request_id(request), exc)
        return JSONResponse(
            status_code=422,
            content=error_payload(
                request, "invalid_request", "The request body does not match the API contract."
            ),
        )

    @app.exception_handler(ModelArtifactUnavailableError)
    async def _model_unavailable(
        request: Request, exc: ModelArtifactUnavailableError
    ) -> JSONResponse:
        logger.error("model unavailable request_id=%s error=%s", request_id(request), exc)
        return JSONResponse(
            status_code=503,
            content=error_payload(
                request, "model_unavailable", "The registered model is temporarily unavailable."
            ),
        )

    @app.exception_handler(ExploreArtifactUnavailableError)
    async def _explore_unavailable(
        request: Request, exc: ExploreArtifactUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_payload(request, "explore_map_unavailable", str(exc)),
        )

    @app.exception_handler(ExploreArtifactCorruptError)
    async def _explore_corrupt(
        request: Request, exc: ExploreArtifactCorruptError
    ) -> JSONResponse:
        logger.error("explore artifact corrupt request_id=%s error=%s", request_id(request), exc)
        return JSONResponse(
            status_code=503,
            content=error_payload(
                request,
                "explore_artifact_unavailable",
                "The reference artifact failed its integrity check.",
            ),
        )

    @app.exception_handler(ExploreContextError)
    async def _invalid_explore_context(
        request: Request, exc: ExploreContextError
    ) -> JSONResponse:
        logger.info("invalid explore context request_id=%s error=%s", request_id(request), exc)
        return JSONResponse(
            status_code=422,
            content=error_payload(
                request,
                "invalid_reference_context",
                "The reference context identifier is invalid.",
            ),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected API error request_id=%s", request_id(request), exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_payload(
                request, "internal_error", "The request could not be completed."
            ),
        )
