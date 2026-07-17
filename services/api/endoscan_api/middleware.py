"""Request IDs and bounded HTTP bodies for safe public API behavior."""

from __future__ import annotations

import re
import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .limits import MAX_REQUEST_BYTES

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestBodyTooLargeError(Exception):
    """The streamed request body exceeded ``MAX_REQUEST_BYTES``."""


class RequestContextMiddleware:
    """Attach a request ID, cap request bytes, and echo the ID on every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = MAX_REQUEST_BYTES + 1
            if declared > MAX_REQUEST_BYTES:
                await self._too_large(request_id, scope, receive, send_with_id)
                return

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > MAX_REQUEST_BYTES:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send_with_id)
        except RequestBodyTooLargeError:
            await self._too_large(request_id, scope, receive, send_with_id)

    @staticmethod
    async def _too_large(request_id: str, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": "request_too_large",
                "detail": (
                    f"Request body exceeds the {MAX_REQUEST_BYTES // (1024 * 1024)} MB limit."
                ),
                "endpoint_id": None,
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)
