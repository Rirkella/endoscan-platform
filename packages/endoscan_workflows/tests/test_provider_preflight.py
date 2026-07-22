from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import SecretStr

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.preflight import ProviderAccessPreflight


class FakeRawResponse:
    def __init__(self, model: str, *, status: int = 200, request_id: str = "req_preflight"):
        self.model = model
        self.http_response = SimpleNamespace(
            status_code=status,
            headers={"x-request-id": request_id},
        )

    def parse(self):
        return SimpleNamespace(id=self.model)


class FakeClient:
    def __init__(self, *, model: str = "gpt-5.4-mini", error: Exception | None = None):
        self.model = model
        self.error = error
        self.retrieve_calls: list[str] = []
        self.closed = False
        self.models = SimpleNamespace(with_raw_response=SimpleNamespace(retrieve=self.retrieve))

    @property
    def responses(self):
        raise AssertionError("Responses generation must never be accessed by preflight")

    def retrieve(self, model: str):
        self.retrieve_calls.append(model)
        if self.error:
            raise self.error
        return FakeRawResponse(self.model)

    def close(self) -> None:
        self.closed = True


def configuration(*, key: bool = True) -> AgentConfiguration:
    return AgentConfiguration(
        provider="openai",
        model="gpt-5.4-mini",
        run_mode=AgentRunMode.LIVE if key else AgentRunMode.REPLAY,
        api_key=SecretStr("unit-test-placeholder") if key else None,
    )


def response(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"x-request-id": f"req_status_{status}"},
        request=httpx.Request("GET", "https://api.openai.com/v1/models/gpt-5.4-mini"),
    )


def api_error(error_class, status: int, code: str, error_type: str):
    return error_class(
        "raw provider message must never be returned",
        response=response(status),
        body={
            "code": code,
            "type": error_type,
            "param": "model",
            "message": "raw response body must never be returned",
        },
    )


def preflight(error: Exception | None = None):
    client = FakeClient(error=error)
    factory_calls: list[dict] = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return client

    return ProviderAccessPreflight(configuration(), client_factory=factory), client, factory_calls


def test_success_retrieves_only_the_configured_model_without_generation() -> None:
    item, client, factory_calls = preflight()
    result = item.check()
    assert result.authentication_accepted is True
    assert result.model_accessible is True
    assert result.http_status == 200
    assert result.request_id == "req_preflight"
    assert result.billing_status == "not_checked"
    assert result.generation_capability == "not_checked"
    assert client.retrieve_calls == ["gpt-5.4-mini"]
    assert client.closed is True
    assert factory_calls[0]["max_retries"] == 0
    assert factory_calls[0]["timeout"] <= 15


@pytest.mark.parametrize(
    ("error", "auth", "status", "code"),
    [
        (
            api_error(AuthenticationError, 401, "invalid_api_key", "authentication_error"),
            False,
            401,
            "invalid_api_key",
        ),
        (
            api_error(PermissionDeniedError, 403, "model_access_denied", "permission_error"),
            True,
            403,
            "model_access_denied",
        ),
        (
            api_error(NotFoundError, 404, "model_not_found", "invalid_request_error"),
            True,
            404,
            "model_not_found",
        ),
        (
            api_error(BadRequestError, 400, "invalid_parameter", "invalid_request_error"),
            True,
            400,
            "invalid_parameter",
        ),
        (
            api_error(RateLimitError, 429, "rate_limit_exceeded", "rate_limit_error"),
            True,
            429,
            "rate_limit_exceeded",
        ),
        (
            api_error(InternalServerError, 500, "server_error", "server_error"),
            False,
            500,
            "server_error",
        ),
    ],
)
def test_safe_status_failures_are_classified_without_raw_content(
    error: Exception, auth: bool, status: int, code: str
) -> None:
    item, client, _factory_calls = preflight(error)
    result = item.check()
    payload = result.model_dump_json()
    assert result.authentication_accepted is auth
    assert result.model_accessible is False
    assert result.http_status == status
    assert result.provider_error_code == code
    assert result.request_id == f"req_status_{status}"
    assert "raw provider" not in payload
    assert "raw response" not in payload
    assert "unit-test-placeholder" not in payload
    assert client.retrieve_calls == ["gpt-5.4-mini"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            APITimeoutError(httpx.Request("GET", "https://api.openai.com/v1/models/x")),
            "provider_timeout",
        ),
        (
            APIConnectionError(request=httpx.Request("GET", "https://api.openai.com/v1/models/x")),
            "provider_connection_error",
        ),
    ],
)
def test_transport_failures_are_safe_and_do_not_retry(error: Exception, code: str) -> None:
    item, client, factory_calls = preflight(error)
    result = item.check()
    assert result.authentication_accepted is False
    assert result.model_accessible is False
    assert result.http_status is None
    assert result.provider_error_code == code
    assert client.retrieve_calls == ["gpt-5.4-mini"]
    assert factory_calls[0]["max_retries"] == 0


def test_missing_key_returns_local_result_without_constructing_client() -> None:
    def forbidden_factory(**_kwargs):
        raise AssertionError("Client must not be constructed without a key")

    result = ProviderAccessPreflight(
        configuration(key=False), client_factory=forbidden_factory
    ).check()
    assert result.api_key_present is False
    assert result.authentication_accepted is False
    assert result.provider_error_code == "missing_api_key"
    assert result.billing_status == "not_checked"
    assert result.generation_capability == "not_checked"


def test_unknown_preflight_exception_is_safe_and_non_generating() -> None:
    item, client, _factory_calls = preflight(
        RuntimeError("arbitrary exception repr with sk-unit-test-secret")
    )
    result = item.check()
    payload = result.model_dump_json()
    assert result.provider_error_code == "preflight_adapter_error"
    assert result.provider_error_type == "local_adapter"
    assert "sk-unit-test-secret" not in payload
    assert client.retrieve_calls == ["gpt-5.4-mini"]
