"""Bounded provider-access preflight with no generation or scientific-source calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from .config import AgentConfiguration
from .contracts import ProviderPreflightResult
from .providers import ProviderFailure

ClientFactory = Callable[..., Any]


class ProviderAccessPreflight:
    """Check configured provider authentication and exact model visibility once."""

    def __init__(
        self,
        configuration: AgentConfiguration,
        *,
        client_factory: ClientFactory = OpenAI,
    ):
        self.configuration = configuration
        self.client_factory = client_factory

    def check(self) -> ProviderPreflightResult:
        if self.configuration.provider != "openai":
            return self._result(
                authentication_accepted=False,
                model_accessible=False,
                provider_error_code="unsupported_provider",
                provider_error_type="local_configuration",
            )
        if not self.configuration.api_key_present:
            return self._result(
                authentication_accepted=False,
                model_accessible=False,
                provider_error_code="missing_api_key",
                provider_error_type="local_configuration",
            )

        client = None
        try:
            client = self.client_factory(
                api_key=self.configuration.api_key.get_secret_value(),
                timeout=min(self.configuration.timeout_seconds, 15.0),
                max_retries=0,
            )
            raw = client.models.with_raw_response.retrieve(self.configuration.model)
            model = raw.parse()
            status = int(raw.http_response.status_code)
            request_id = raw.http_response.headers.get("x-request-id")
            if getattr(model, "id", None) != self.configuration.model:
                return self._result(
                    authentication_accepted=True,
                    model_accessible=False,
                    http_status=status,
                    provider_error_code="configured_model_mismatch",
                    provider_error_type="local_response_validation",
                    request_id=request_id,
                )
            return self._result(
                authentication_accepted=True,
                model_accessible=True,
                http_status=status,
                request_id=request_id,
            )
        except APITimeoutError as exc:
            return self._exception_result(
                exc,
                authentication_accepted=False,
                provider_error_code="provider_timeout",
                provider_error_type="transport_error",
            )
        except RateLimitError as exc:
            return self._exception_result(exc, authentication_accepted=True)
        except APIConnectionError as exc:
            return self._exception_result(
                exc,
                authentication_accepted=False,
                provider_error_code="provider_connection_error",
                provider_error_type="transport_error",
            )
        except APIStatusError as exc:
            return self._exception_result(
                exc,
                authentication_accepted=exc.status_code not in {401} and exc.status_code < 500,
            )
        except Exception as exc:
            return self._exception_result(
                exc,
                authentication_accepted=False,
                provider_error_code="preflight_adapter_error",
                provider_error_type="local_adapter",
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _exception_result(
        self,
        exc: Exception,
        *,
        authentication_accepted: bool,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
    ) -> ProviderPreflightResult:
        details = ProviderFailure(
            "Provider preflight failed safely.",
            retryable=False,
            exception_class=type(exc).__name__,
            http_status=getattr(exc, "status_code", None),
            provider_error_code=getattr(exc, "code", None) or provider_error_code,
            provider_error_type=getattr(exc, "type", None) or provider_error_type,
            provider_request_id=getattr(exc, "request_id", None),
            provider_parameter=getattr(exc, "param", None),
        ).trace_detail
        return self._result(
            authentication_accepted=authentication_accepted,
            model_accessible=False,
            http_status=details.get("http_status"),
            provider_error_code=details.get("provider_error_code"),
            provider_error_type=details.get("provider_error_type"),
            request_id=details.get("provider_request_id"),
        )

    def _result(self, **updates: Any) -> ProviderPreflightResult:
        payload = {
            "provider": self.configuration.provider,
            "configured_model": self.configuration.model,
            "api_key_present": self.configuration.api_key_present,
            "authentication_accepted": False,
            "model_accessible": False,
        }
        payload.update(updates)
        return ProviderPreflightResult(**payload)
