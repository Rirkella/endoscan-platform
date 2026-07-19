"""OpenAI Agents SDK adapter that leaves EndoScan's harness authoritative."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from agents import (
    Agent,
    AgentOutputSchema,
    FunctionTool,
    ModelSettings,
    OpenAIProvider,
    RunConfig,
    RunErrorHandlerResult,
    Runner,
)
from agents.exceptions import (
    AgentsException,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    UserError,
)
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from .config import AgentConfiguration
from .contracts import (
    AgentRunRequest,
    NormalizedAgentError,
    ProviderToolRequest,
    ProviderTurn,
    StructuredOutputDiagnostic,
    StructuredOutputRequestFingerprint,
    UsageReport,
)
from .discovery import DiscoveryOutput
from .providers import ProviderFailure, ProviderTimeout
from .repository import canonical_json
from .tools import ToolRegistry
from .training_dataset import (
    DatasetSpecificationAgentOutcome,
    DatasetSpecificationReviewOutcome,
    TrainingDatasetAssemblyReview,
    VerifiedSourceInventoryFragment,
)

TOOL_ENVELOPE = "__endoscan_tool_request__"

OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    DiscoveryOutput.__name__: DiscoveryOutput,
    DatasetSpecificationAgentOutcome.__name__: DatasetSpecificationAgentOutcome,
    DatasetSpecificationReviewOutcome.__name__: DatasetSpecificationReviewOutcome,
    VerifiedSourceInventoryFragment.__name__: VerifiedSourceInventoryFragment,
    TrainingDatasetAssemblyReview.__name__: TrainingDatasetAssemblyReview,
}


def resolve_output_schema(name: str) -> type[BaseModel]:
    try:
        return OUTPUT_SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported structured output schema: {name}") from exc


def sdk_output_schema(name: str):
    schema = resolve_output_schema(name)
    if schema is DiscoveryOutput:
        return schema
    strict = schema in {
        DatasetSpecificationAgentOutcome,
        DatasetSpecificationReviewOutcome,
        TrainingDatasetAssemblyReview,
        VerifiedSourceInventoryFragment,
    }
    return AgentOutputSchema(schema, strict_json_schema=strict)


_SAFE_MODEL_BEHAVIOR_PATTERNS = (
    (re.compile(r"refus", re.I), "model_refusal", "The model refused the structured request."),
    (
        re.compile(r"incomplete|length|token limit", re.I),
        "response_incomplete",
        "The provider response was incomplete before structured validation finished.",
    ),
    (
        re.compile(r"validat|schema|required|extra field", re.I),
        "schema_validation_failed",
        "The model response did not match the required structured schema.",
    ),
    (
        re.compile(r"json|decode|parse", re.I),
        "malformed_json",
        "The model response was not valid structured JSON.",
    ),
    (
        re.compile(r"tool", re.I),
        "unexpected_tool_call",
        "The model emitted a tool request where a final structured outcome was required.",
    ),
    (
        re.compile(r"final output|structured output|no output|empty", re.I),
        "missing_structured_output",
        "The model did not provide a structured final outcome.",
    ),
)


def classify_model_behavior(message: object | None, *, refusal: bool = False) -> tuple[str, str]:
    """Classify known SDK behavior without retaining arbitrary model content."""

    if refusal:
        return "model_refusal", "The model refused the structured request."
    text = str(message or "")[:4000]
    for pattern, category, safe_message in _SAFE_MODEL_BEHAVIOR_PATTERNS:
        if pattern.search(text):
            return category, safe_message
    return "unknown_model_behavior", "The model produced an invalid structured outcome."


class OpenAIAgentProvider:
    """Translate one SDK model turn into one EndoScan provider-neutral turn.

    SDK function tools are request proxies only. They never execute scientific work;
    EndoScan's harness receives the requested name/arguments, enforces policy, executes
    the registered tool, persists the result, and supplies it on the next provider turn.
    """

    name = "openai"

    @staticmethod
    def sdk_version() -> str:
        try:
            return version("openai-agents")
        except PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _failure(
        exc: Exception,
        message: str,
        *,
        retryable: bool,
    ) -> ProviderFailure:
        return ProviderFailure(
            message,
            retryable=retryable,
            exception_class=type(exc).__name__,
            http_status=getattr(exc, "status_code", None),
            provider_error_code=getattr(exc, "code", None),
            provider_error_type=getattr(exc, "type", None),
            provider_request_id=getattr(exc, "request_id", None),
            provider_parameter=getattr(exc, "param", None),
        )

    def _user_error_failure(self, exc: UserError, request: AgentRunRequest) -> ProviderFailure:
        return ProviderFailure(
            "OpenAI Agents SDK configuration was rejected locally.",
            retryable=False,
            exception_class=type(exc).__name__,
            developer_message=exc.message,
            sdk_version=self.sdk_version(),
            adapter_operation="run_turn",
            adapter_model=request.model.model_identifier,
            adapter_max_turns=1,
            adapter_tool_count=len(request.available_tools),
            adapter_output_schema=request.output_schema_name,
            adapter_use_responses=True,
            adapter_parallel_tool_calls=False,
            adapter_store=False,
            adapter_tracing_disabled=not self.configuration.tracing_enabled,
        )

    def __init__(
        self,
        configuration: AgentConfiguration,
        tools: ToolRegistry,
        *,
        runner: Callable[..., Any] | None = None,
    ):
        self.configuration = configuration
        self.tools = tools
        self.runner = runner or Runner.run_sync

    def structured_output_fingerprint(
        self, request: AgentRunRequest
    ) -> StructuredOutputRequestFingerprint:
        output_type = sdk_output_schema(request.output_schema_name)
        output_schema = (
            output_type
            if isinstance(output_type, AgentOutputSchema)
            else AgentOutputSchema(output_type)
        )
        schema = output_schema.json_schema()
        encoded_schema = canonical_json(schema).encode()
        strict = output_schema.is_strict_json_schema()
        settings = {
            "parallel_tool_calls": False,
            "max_tokens": request.budget.maximum_output_tokens,
            "store": False,
            "verbosity": "low",
            "tool_use_behavior": "stop_on_first_tool",
        }
        configuration = {
            "agent_name": request.agent_name,
            "output_type_name": request.output_schema_name,
            "schema_hash": hashlib.sha256(encoded_schema).hexdigest(),
            "strict_json_schema": strict,
            "api_surface": "responses",
            "configured_model": request.model.model_identifier,
            "tool_count": len(request.available_tools),
            "tool_choice_mode": "auto" if request.available_tools else "none",
            "model_settings": settings,
        }
        runtime_hash = hashlib.sha256(canonical_json(configuration).encode()).hexdigest()
        expected_contract = request.context.get("structured_output_boundary_contract")
        boundary_configuration = configuration
        if isinstance(expected_contract, dict):
            expected_schema_name = str(
                expected_contract.get("output_schema_name", request.output_schema_name)
            )
            expected_output_type = sdk_output_schema(expected_schema_name)
            expected_output_schema = (
                expected_output_type
                if isinstance(expected_output_type, AgentOutputSchema)
                else AgentOutputSchema(expected_output_type)
            )
            expected_schema = canonical_json(expected_output_schema.json_schema()).encode()
            boundary_configuration = {
                **configuration,
                "agent_name": expected_contract.get("agent_name"),
                "output_type_name": expected_schema_name,
                "schema_hash": hashlib.sha256(expected_schema).hexdigest(),
                "api_surface": expected_contract.get("api_surface"),
                "configured_model": expected_contract.get("configured_model"),
                "tool_count": expected_contract.get("tool_count"),
                "tool_choice_mode": expected_contract.get("tool_choice_mode"),
            }
        boundary_hash = hashlib.sha256(canonical_json(boundary_configuration).encode()).hexdigest()
        return StructuredOutputRequestFingerprint(
            agent_name=request.agent_name,
            output_type_name=request.output_schema_name,
            schema_hash=configuration["schema_hash"],
            schema_byte_size=len(encoded_schema),
            strict_json_schema=strict,
            api_surface="responses",
            provider_class=type(self).__name__,
            configured_model=request.model.model_identifier,
            tool_count=len(request.available_tools),
            tool_choice_mode="auto" if request.available_tools else "none",
            sdk_version=self.sdk_version(),
            model_settings_fingerprint=hashlib.sha256(
                canonical_json(settings).encode()
            ).hexdigest(),
            boundary_probe_configuration_hash=boundary_hash,
            runtime_configuration_hash=runtime_hash,
            boundary_runtime_contracts_match=boundary_hash == runtime_hash,
            output_type_present=output_type is not None,
        )

    def run_turn(
        self,
        request: AgentRunRequest,
        history: list[dict],
        *,
        interruption_requested: bool,
    ) -> ProviderTurn:
        if interruption_requested:
            return ProviderTurn(
                kind="error",
                error=NormalizedAgentError(
                    code="interrupted",
                    safe_message="Agent run was interrupted by the EndoScan orchestrator.",
                    retryable=False,
                    category="interruption",
                ),
            )
        if not self.configuration.api_key_present:
            raise ProviderFailure(
                "OpenAI provider is not configured.",
                retryable=False,
                exception_class="ProviderConfigurationError",
                provider_error_code="missing_api_key",
                provider_error_type="local_configuration",
            )
        started = time.monotonic()
        handler_capture: dict[str, StructuredOutputDiagnostic] = {}
        try:
            agent = self._build_agent(request)
            run_config = self._build_run_config(request)
            runner_kwargs: dict[str, Any] = {
                "max_turns": 1,
                "run_config": run_config,
            }
            handlers = self._error_handlers(request, handler_capture, started)
            if handlers:
                runner_kwargs["error_handlers"] = handlers
            result = self.runner(agent, self._turn_input(request, history), **runner_kwargs)
        except (APITimeoutError, TimeoutError) as exc:
            raise ProviderTimeout(
                "OpenAI provider exceeded the configured timeout.",
                exception_class=type(exc).__name__,
            ) from exc
        except RateLimitError as exc:
            raise self._failure(exc, "OpenAI provider rate limit reached.", retryable=True) from exc
        except APIConnectionError as exc:
            raise self._failure(exc, "OpenAI provider is unavailable.", retryable=True) from exc
        except APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in {408, 429}
            raise self._failure(
                exc, "OpenAI provider returned an API error.", retryable=retryable
            ) from exc
        except MaxTurnsExceeded as exc:
            raise self._failure(
                exc,
                "OpenAI provider exceeded the single-turn adapter boundary.",
                retryable=False,
            ) from exc
        except ModelBehaviorError as exc:
            diagnostic = self._structured_output_diagnostic(
                exc,
                getattr(exc, "run_data", None),
                request,
                started,
                error_handler="none",
            )
            raise ProviderFailure(
                "OpenAI provider returned malformed structured output.",
                retryable=False,
                exception_class=type(exc).__name__,
                developer_message=diagnostic.developer_message,
                sdk_version=diagnostic.sdk_version,
                adapter_operation="run_turn",
                adapter_model=request.model.model_identifier,
                adapter_max_turns=1,
                adapter_tool_count=len(request.available_tools),
                adapter_output_schema=request.output_schema_name,
                structured_output_diagnostic=diagnostic,
            ) from exc
        except ModelRefusalError as exc:
            raise self._failure(
                exc,
                "OpenAI provider refused the structured request.",
                retryable=False,
            ) from exc
        except UserError as exc:
            raise self._user_error_failure(exc, request) from exc
        except AgentsException as exc:
            raise self._failure(
                exc, "OpenAI Agents SDK run failed safely.", retryable=False
            ) from exc
        except Exception as exc:
            raise self._failure(
                exc, "OpenAI provider adapter failed safely.", retryable=False
            ) from exc

        try:
            return self._translate_result(result, diagnostic=handler_capture.get("diagnostic"))
        except ProviderFailure:
            raise
        except Exception as exc:
            raise self._failure(
                exc, "OpenAI provider adapter failed safely.", retryable=False
            ) from exc

    def _translate_result(
        self, result: Any, *, diagnostic: StructuredOutputDiagnostic | None = None
    ) -> ProviderTurn:
        usage = self._usage(result)
        output = result.final_output
        if hasattr(output, "model_dump"):
            return ProviderTurn(
                kind="output",
                output=output.model_dump(mode="json"),
                usage=usage,
                diagnostic=diagnostic,
            )
        if isinstance(output, str):
            try:
                oã{¶‰žËkºwµçQÑÈ¡¥Ñ•´°€‰½ÕÑÁÕÑ}Ñ½­•¹Ìˆ°€À¤½È€À¤™½È¥Ñ•´¥¸…Ù…¥±…‰±”¤(€€€€€€€…¡•‘}Ñ½­•¹Ì€ôÍÕ´ (€€€€€€€€€€€¥¹Ð¡•Ñ…ÑÑÈ¡•Ñ…ÑÑÈ¡¥Ñ•´°€‰¥¹ÁÕÑ}Ñ½­•¹Í}‘•Ñ…¥±Ìˆ°9½¹”¤°€‰…¡•‘}Ñ½­•¹Ìˆ°€À¤½È€À¤(€€€€€€€€€€€™½È¥Ñ•´¥¸…Ù…¥±…‰±”(€€€€€€€€¤(€€€€€€€½ÍÑ}ÕÍ€ô€ (€€€€€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ì€¨Í•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹¥¹ÁÕÑ}½ÍÑ}Á•É}µ¥±±¥½¹}ÕÍ(€€€€€€€€€€€€¬½ÕÑÁÕÑ}Ñ½­•¹Ì€¨Í•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹½ÕÑÁÕÑ}½ÍÑ}Á•É}µ¥±±¥½¹}ÕÍ(€€€€€€€€¤€¼€Å|ÀÀÁ|ÀÀÀ(€€€€€€€É•ÑÕÉ¸UÍ…•I•Á½ÉÐ (€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌô (€€€€€€€€€€€€€€€€‰ÕÍ…•}É•½É‘•ˆ¥˜±•¸¡…Ù…¥±…‰±”¤€ôô±•¸¡É•ÍÁ½¹Í•Ì¤•±Í”€‰ÕÍ…•}Á…ÉÑ¥…°ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ìõ¥¹ÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€½ÕÑÁÕÑ}Ñ½­•¹Ìõ½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€…¡•‘}Ñ½­•¹Ìõ…¡•‘}Ñ½­•¹Ì°(€€€€€€€€€€€½ÍÑ}•¹ÑÌõ½ÍÑ}ÕÍ€¨€ÄÀÀ°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘ÌõÍ•±˜¹}Í…™•}¥‘Ì¡É•ÍÁ½¹Í•Ì°€‰É•ÅÕ•ÍÑ}¥ˆ¤°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘ÌõÍ•±˜¹}Í…™•}¥‘Ì¡É•ÍÁ½¹Í•Ì°€‰É•ÍÁ½¹Í•}¥ˆ¤°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ìõ±•¸¡É•ÍÁ½¹Í•Ì¤°(€€€€€€€€¤((€€€‘•˜}‰Õ¥±‘}…•¹Ð¡Í•±˜°É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ¤€´ø•¹Ðè(€€€€€€€½ÕÑÁÕÑ}ÑåÁ”€ôÍ‘­}½ÕÑÁÕÑ}Í¡•µ„¡É•ÅÕ•ÍÐ¹½ÕÑÁÕÑ}Í¡•µ…}¹…µ”¤(€€€€€€€¥˜½ÕÑÁÕÑ}ÑåÁ”¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”UÍ•ÉÉÉ½È ‰MÑÉÕÑÕÉ•½ÕÑÁÕÑ}ÑåÁ”¥ÌÉ•ÅÕ¥É•™½ÈÁÉ½‘ÕÑ¥½¸…•¹ÑÌ¸ˆ¤(€€€€€€€É•ÑÕÉ¸•¹Ð (€€€€€€€€€€€¹…µ”õÉ•ÅÕ•ÍÐ¹…•¹Ñ}¹…µ”°(€€€€€€€€€€€¥¹ÍÑÉÕÑ¥½¹ÌõÉ•ÅÕ•ÍÐ¹¥¹ÍÑÉÕÑ¥½¹Ì°(€€€€€€€€€€€µ½‘•°õÉ•ÅÕ•ÍÐ¹µ½‘•°¹µ½‘•±}¥‘•¹Ñ¥™¥•È°(€€€€€€€€€€€µ½‘•±}Í•ÑÑ¥¹Ìõ5½‘•±M•ÑÑ¥¹Ì (€€€€€€€€€€€€€€€Á…É…±±•±}Ñ½½±}…±±Ìõ…±Í”°(€€€€€€€€€€€€€€€µ…á}Ñ½­•¹ÌõÉ•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€€€€ÍÑ½É”õ…±Í”°(€€€€€€€€€€€€€€€Ù•É‰½Í¥Ñäô‰±½Üˆ°(€€€€€€€€€€€€¤°(€€€€€€€€€€€Ñ½½±ÌõmÍ•±˜¹}ÁÉ½áå}Ñ½½°¡¹…µ”¤™½È¹…µ”¥¸É•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ít°(€€€€€€€€€€€½ÕÑÁÕÑ}ÑåÁ”õ½ÕÑÁÕÑ}ÑåÁ”°(€€€€€€€€€€€Ñ½½±}ÕÍ•}‰•¡…Ù¥½Èô‰ÍÑ½Á}½¹}™¥ÉÍÑ}Ñ½½°ˆ°(€€€€€€€€¤((€€€‘•˜}‰Õ¥±‘}ÉÕ¹}½¹™¥œ (€€€€€€€Í•±˜°É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°€¨°µ½‘•±}ÁÉ½Ù¥‘•Èè¹äð9½¹”€ô9½¹”(€€€€¤€´øIÕ¹½¹™¥œè(€€€€€€€¥˜µ½‘•±}ÁÉ½Ù¥‘•È¥Ì9½¹”è(€€€€€€€€€€€±¥•¹Ð€ôÍå¹=Á•¹$ (€€€€€€€€€€€€€€€…Á¥}­•äõÍ•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹…Á¥}­•ä¹•Ñ}Í•É•Ñ}Ù…±Õ” ¤°(€€€€€€€€€€€€€€€Ñ¥µ•½ÕÐõÉ•ÅÕ•ÍÐ¹‰Õ‘•Ð¹Ñ¥µ•½ÕÑ}Í•½¹‘Ì°(€€€€€€€€€€€€€€€µ…á}É•ÑÉ¥•ÌôÀ°(€€€€€€€€€€€€¤(€€€€€€€€€€€µ½‘•±}ÁÉ½Ù¥‘•È€ô=Á•¹%AÉ½Ù¥‘•È¡½Á•¹…¥}±¥•¹Ðõ±¥•¹Ð°ÕÍ•}É•ÍÁ½¹Í•ÌõQÉÕ”¤(€€€€€€€É•ÑÕÉ¸IÕ¹½¹™¥œ (€€€€€€€€€€€µ½‘•±}ÁÉ½Ù¥‘•Èõµ½‘•±}ÁÉ½Ù¥‘•È°(€€€€€€€€€€€ÑÉ…¥¹}‘¥Í…‰±•õ¹½ÐÍ•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹ÑÉ…¥¹}•¹…‰±•°(€€€€€€€€€€€ÑÉ…•}¥¹±Õ‘•}Í•¹Í¥Ñ¥Ù•}‘…Ñ„õ…±Í”°(€€€€€€€€€€€Ý½É­™±½Ý}¹…µ”ô‰¹‘½M…¸‘…Ñ…Í•Ð‘¥Í½Ù•Éäˆ°(€€€€€€€€€€€É½ÕÁ}¥õÉ•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°(€€€€€€€€€€€ÑÉ…•}µ•Ñ…‘…Ñ„õì(€€€€€€€€€€€€€€€€‰•¹‘½Í…¹}Ý½É­™±½Ý}¥ˆèÉ•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°(€€€€€€€€€€€€€€€€‰•¹‘½Í…¹}ÍÑ•Á}¥ˆèÉ•ÅÕ•ÍÐ¹ÍÑ•Á}¥°(€€€€€€€€€€€€€€€€‰…•¹Ñ}Ù•ÉÍ¥½¸ˆèÉ•ÅÕ•ÍÐ¹…•¹Ñ}Ù•ÉÍ¥½¸°(€€€€€€€€€€€ô°(€€€€€€€€¤((€€€‘•˜}ÁÉ½áå}Ñ½½°¡Í•±˜°¹…µ”èÍÑÈ¤€´øÕ¹Ñ¥½¹Q½½°è(€€€€€€€É•¥ÍÑ•É•€ôÍ•±˜¹Ñ½½±Ì¹•Ð¡¹…µ”¤((€€€€€€€…Íå¹Œ‘•˜É•ÅÕ•ÍÑ}½¹±ä¡}½¹Ñ•áÐ°É…Ý}…ÉÕµ•¹ÑÌèÍÑÈ¤€´øÍÑÈè(€€€€€€€€€€€…ÉÕµ•¹ÑÌ€ô©Í½¸¹±½…‘Ì¡É…Ý}…ÉÕµ•¹ÑÌ¤(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ (€€€€€€€€€€€€€€€íQ==1}9Y1=AèQÉÕ”°€‰Ñ½½±}¹…µ”ˆè¹…µ”°€‰…ÉÕµ•¹ÑÌˆè…ÉÕµ•¹ÑÍô°(€€€€€€€€€€€€€€€Í½ÉÑ}­•åÌõQÉÕ”°(€€€€€€€€€€€€¤((€€€€€€€É•ÑÕÉ¸Õ¹Ñ¥½¹Q½½° (€€€€€€€€€€€¹…µ”õ¹…µ”°(€€€€€€€€€€€‘•ÍÉ¥ÁÑ¥½¸õÉ•¥ÍÑ•É•¹‘•™¥¹¥Ñ¥½¸¹‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€€€€€Á…É…µÍ}©Í½¹}Í¡•µ„õÉ•¥ÍÑ•É•¹¥¹ÁÕÑ}µ½‘•°¹µ½‘•±}©Í½¹}Í¡•µ„ ¤°(€€€€€€€€€€€½¹}¥¹Ù½­•}Ñ½½°õÉ•ÅÕ•ÍÑ}½¹±ä°(€€€€€€€€€€€ÍÑÉ¥Ñ}©Í½¹}Í¡•µ„õ¹½ÐÉ•¥ÍÑ•É•¹‘•™¥¹¥Ñ¥½¸¹¥µÁ±•µ•¹Ñ…Ñ¥½¹}Ù•ÉÍ¥½¸¹ÍÑ…ÉÑÍÝ¥Ñ  (€€€€€€€€€€€€€€€€‰ÑÉ…¥¹¥¹œµ‘…Ñ…Í•Ðˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€Ñ¥µ•½ÕÑ}Í•½¹‘ÌõÉ•¥ÍÑ•É•¹‘•™¥¹¥Ñ¥½¸¹Ñ¥µ•½ÕÑ}Í•½¹‘Ì°(€€€€€€€€€€€Ñ¥µ•½ÕÑ}‰•¡…Ù¥½Èô‰É…¥Í•}•á•ÁÑ¥½¸ˆ°(€€€€€€€€¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}ÑÕÉ¹}¥¹ÁÕÐ¡É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°¡¥ÍÑ½Éäè±¥ÍÑm‘¥Ñt¤€´øÍÑÈè(€€€€€€€Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌ€ôÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌˆ°íô¤(€€€€€€€•¹½‘•‘}…ÉÑ¥™…ÑÌ€ô…¹½¹¥…±}©Í½¸¡Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌ¤(€€€€€€€¥˜±•¸¡•¹½‘•‘}…ÉÑ¥™…ÑÌ¤€ø€ÈÑ|ÀÀÀè(€€€€€€€€€€€Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌ€ôì(€€€€€€€€€€€€€€€€‰½¹Ñ•áÑ}ÑÉÕ¹…Ñ•ˆèQÉÕ”°(€€€€€€€€€€€€€€€€‰…Ù…¥±…‰±•}…ÉÑ¥™…Ñ}¹…µ•ÌˆèÍ½ÉÑ•¡Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌ¤°(€€€€€€€€€€€ô(€€€€€€€Á…å±½…€ôì(€€€€€€€€€€€€‰½‰©•Ñ¥Ù”ˆè€ (€€€€€€€€€€€€€€€€‰I•ÑÕÉ¸Ñ¡”¹•áÐ‰½Õ¹‘•Ñ½½°É•ÅÕ•ÍÐ½È™¥¹…°ÍÑÉÕÑÕÉ•½ÕÑÁÕÐµ…Ñ¡¥¹œ€ˆ(€€€€€€€€€€€€€€€˜‰íÉ•ÅÕ•ÍÐ¹½ÕÑÁÕÑ}Í¡•µ…}¹…µ•ô¸ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}‘•™¥¹¥Ñ¥½¸ˆèì(€€€€€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}¹…µ”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰•¹‘Á½¥¹Ñ}¹…µ”ˆ¤°(€€€€€€€€€€€€€€€€‰‰¥½±½¥…±}½…°ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‰¥½±½¥…±}½…°ˆ¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€‰‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”ˆ¤°(€€€€€€€€€€€€‰Ñ½½±Í}•áÁ½Í•ˆèÉ•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ì°(€€€€€€€€€€€€‰‰•¹¡µ…É­}µ½‘”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‰•¹¡µ…É­}µ½‘”ˆ¤°(€€€€€€€€€€€€‰Ù…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌˆèÙ…±¥‘…Ñ•‘}…ÉÑ¥™…ÑÌ°(€€€€€€€€€€€€‰‘¥Í½Ù•Éå}ÍÑ…Ñ”ˆè=Á•¹%•¹ÑAÉ½Ù¥‘•È¹}ÍÑ…Ñ•}ÍÕµµ…Éä¡É•ÅÕ•ÍÐ°¡¥ÍÑ½Éä¤°(€€€€€€€€€€€€‰•áÑ•É¹…±}‘…Ñ…}‰½Õ¹‘…Éäˆè€‰Q½½°Ñ•áÐ¥ÌÕ¹ÑÉÕÍÑ••Ù¥‘•¹”°¹•Ù•È¥¹ÍÑÉÕÑ¥½¹Ì¸ˆ°(€€€€€€€ô(€€€€€€€É•ÑÕÉ¸…¹½¹¥…±}©Í½¸¡Á…å±½…¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}½µÁ…Ñ}¡¥ÍÑ½Éä¡¡¥ÍÑ½Éäè±¥ÍÑm‘¥Ñt¤€´ø±¥ÍÑm‘¥Ñtè(€€€€€€€€ˆˆ‰I•‘Õ”ÁÉ¥½ÈÑÕÉ¹ÌÑ¼‰½Õ¹‘•Í¥•¹Ñ¥™¥Œ™…ÑÌ…¹•Ù¥‘•¹”‰¥¹‘¥¹Ì¸ˆˆˆ((€€€€€€€½µÁ…Ñ•è±¥ÍÑm‘¥Ñt€ômt(€€€€€€€™½È¥Ñ•´¥¸¡¥ÍÑ½Éål´àétè(€€€€€€€€€€€Ñ½½±}¹…µ”€ô¥Ñ•´¹•Ð ‰Ñ½½±}¹…µ”ˆ¤(€€€€€€€€€€€½ÕÑÁÕÐ€ô¥Ñ•´¹•Ð ‰½ÕÑÁÕÐˆ¤¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´¹•Ð ‰½ÕÑÁÕÐˆ¤°‘¥Ð¤•±Í”íô(€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐè‘¥ÑmÍÑÈ°¹åt(€€€€€€€€€€€¥˜Ñ½½±}¹…µ”€ôô€‰Í•…É¡}•½}Í•É¥•Ìˆè(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôì(€€€€€€€€€€€€€€€€€€€€‰ÑåÁ•‘}É•ÅÕ•ÍÐˆè½ÕÑÁÕÐ¹•Ð ‰ÑåÁ•‘}É•ÅÕ•ÍÐˆ¤°(€€€€€€€€€€€€€€€€€€€€‰É•¹‘•É•‘}ÅÕ•Éäˆè½ÕÑÁÕÐ¹•Ð ‰É•¹‘•É•‘}ÅÕ•Éäˆ¤°(€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±Ñ}½Õ¹Ðˆè½ÕÑÁÕÐ¹•Ð ‰É•ÍÕ±Ñ}½Õ¹Ðˆ°€À¤°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…ÉÑ¥™…Ñ}¥ˆè½ÕÑÁÕÐ¹•Ð ‰Í½ÕÉ•}…ÉÑ¥™…Ñ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±ÑÌˆèl(€€€€€€€€€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€­•äèÉ•ÍÕ±Ð¹•Ð¡­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È­•ä¥¸€ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…•ÍÍ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½É…¹¥Í´ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÕ‘å}ÑåÁ”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í…µÁ±•}½Õ¹Ðˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸½ÕÑÁÕÐ¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¥lèÕt(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É•ÍÕ±Ð°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆè(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôì(€€€€€€€€€€€€€€€€€€€€‰ÁÕ‰±¥}Ù…±¥‘}½Õ¹Ðˆè½ÕÑÁÕÐ¹•Ð ‰ÁÕ‰±¥}Ù…±¥‘}½Õ¹Ðˆ°€À¤°(€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±ÑÌˆèl(€€€€€€€€€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€­•äèÉ•ÍÕ±Ð¹•Ð¡­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È­•ä¥¸€ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…•ÍÍ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½É…¹¥Í´ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÕ‘å}ÑåÁ”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…ÉÑ¥™…Ñ}¥ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}É•™•É•¹•Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸½ÕÑÁÕÐ¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¥lèÕt(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É•ÍÕ±Ð°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆè(€€€€€€€€€€€€€€€…±±½Ý•€ôì(€€€€€€€€€€€€€€€€€€€€‰…•ÍÍ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆ°(€€€€€€€€€€€€€€€€€€€€‰Ù•É¥™¥•‘}Ñ¥Ñ±”ˆ°(€€€€€€€€€€€€€€€€€€€€‰½É…¹¥Í´ˆ°(€€€€€€€€€€€€€€€€€€€€‰ÍÑÕ‘å}ÑåÁ”ˆ°(€€€€€€€€€€€€€€€€€€€€‰Í…µÁ±•}½Õ¹Ðˆ°(€€€€€€€€€€€€€€€€€€€€‰‰¥½±½¥…±}½¹Ñ•áÐˆ°(€€€€€€€€€€€€€€€€€€€€‰•±±}±¥¹•Í}½É}Ñ¥ÍÍÕ•Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰ÑÉ•…Ñµ•¹Ñ}É½ÕÁÌˆ°(€€€€€€€€€€€€€€€€€€€€‰±¥­•±å}½¹ÑÉ½±}É½ÕÁÌˆ°(€€€€€€€€€€€€€€€€€€€€‰É•Á±¥…Ñ•}¥¹™½Éµ…Ñ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€‰‘½Í•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€€€€€€€€€‰±¥¹­•‘}ÁÕ‰±¥…Ñ¥½¹}¥‘Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰µ•Ñ…‘…Ñ…}½µÁ±•Ñ•¹•ÍÌˆ°(€€€€€€€€€€€€€€€€€€€€‰•áÁ±¥¥Ñ}Õ¹•ÉÑ…¥¹Ñ¥•Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…ÉÑ¥™…Ñ}É•™•É•¹•Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}É•™•É•¹•Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰Í…™•}•ÉÉ½É}…Ñ•½Éäˆ°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôì(€€€€€€€€€€€€€€€€€€€€‰¥¹ÍÁ•Ñ•‘}½Õ¹Ðˆè½ÕÑÁÕÐ¹•Ð ‰¥¹ÍÁ•Ñ•‘}½Õ¹Ðˆ°€À¤°(€€€€€€€€€€€€€€€€€€€€‰™…¥±•‘}½Õ¹Ðˆè½ÕÑÁÕÐ¹•Ð ‰™…¥±•‘}½Õ¹Ðˆ°€À¤°(€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±ÑÌˆèl(€€€€€€€€€€€€€€€€€€€€€€€í­•äèÙ…±Õ”™½È­•ä°Ù…±Õ”¥¸É•ÍÕ±Ð¹¥Ñ•µÌ ¤¥˜­•ä¥¸…±±½Ý•‘ô(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸½ÕÑÁÕÐ¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¥lèÕt(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É•ÍÕ±Ð°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰½µÁ…É•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ìˆè(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôì(€€€€€€€€€€€€€€€€€€€€‰½µÁ…É¥Í½¹}™¥•±‘Ìˆè½ÕÑÁÕÐ¹•Ð ‰½µÁ…É¥Í½¹}™¥•±‘Ìˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•Ìˆè½ÕÑÁÕÐ¹•Ð ‰…¹‘¥‘…Ñ•Ìˆ°mt¥lèÕt°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰™•Ñ¡}ÁÕ‰±¥…Ñ¥½¹}µ•Ñ…‘…Ñ„ˆè(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôì(€€€€€€€€€€€€€€€€€€€€‰ÁÕ‰±¥…Ñ¥½¹Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Áµ¥ˆèÁÕ‰±¥…Ñ¥½¸¹•Ð ‰Áµ¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆèÍÑÈ¡ÁÕ‰±¥…Ñ¥½¸¹•Ð ‰Ñ¥Ñ±”ˆ°€ˆˆ¤¥lèÌÀÁt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…‰ÍÑÉ…ÐˆèÍÑÈ¡ÁÕ‰±¥…Ñ¥½¸¹•Ð ‰…‰ÍÑÉ…Ðˆ°€ˆˆ¤¥lèÔÀÁt°(€€€€€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÁÕ‰±¥…Ñ¥½¸¥¸½ÕÑÁÕÐ¹•Ð ‰ÁÕ‰±¥…Ñ¥½¹Ìˆ°mt¥lèÍt(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ÁÕ‰±¥…Ñ¥½¸°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}É•™•É•¹•Ìˆè½ÕÑÁÕÐ¹•Ð ‰•Ù¥‘•¹•}É•™•É•¹•Ìˆ°mt¥lèÙt°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€½µÁ…Ñ}½ÕÑÁÕÐ€ôíô(€€€€€€€€€€€½µÁ…Ñ•¹…ÁÁ•¹ (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰Ñ½½±}¹…µ”ˆèÑ½½±}¹…µ”°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè¥Ñ•´¹•Ð ‰ÍÑ…ÑÕÌˆ¤°(€€€€€€€€€€€€€€€€€€€€‰½ÕÑÁÕÐˆè½µÁ…Ñ}½ÕÑÁÕÐ°(€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè¥Ñ•´¹•Ð ‰•ÉÉ½Èˆ¤°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸½µÁ…Ñ•((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}ÍÑ…Ñ•}ÍÕµµ…Éä¡É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°¡¥ÍÑ½Éäè±¥ÍÑm‘¥Ñt¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€½µÁ…Ð€ô=Á•¹%•¹ÑAÉ½Ù¥‘•È¹}½µÁ…Ñ}¡¥ÍÑ½Éä¡¡¥ÍÑ½Éä¤(€€€€€€€Í•…É¡•Ì€ôm¥Ñ•µl‰½ÕÑÁÕÐ‰t™½È¥Ñ•´¥¸½µÁ…Ð¥˜¥Ñ•µl‰Ñ½½±}¹…µ”‰t€ôô€‰Í•…É¡}•½}Í•É¥•Ì‰t(€€€€€€€Ù…±¥‘…Ñ¥½¹Ì€ôl(€€€€€€€€€€€É•ÍÕ±Ð(€€€€€€€€€€€™½È¥Ñ•´¥¸½µÁ…Ð(€€€€€€€€€€€¥˜¥Ñ•µl‰Ñ½½±}¹…µ”‰t€ôô€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ(€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸¥Ñ•µl‰½ÕÑÁÕÐ‰t¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¤(€€€€€€€ulèÕt(€€€€€€€¥¹ÍÁ•Ñ¥½¹Ì€ôl(€€€€€€€€€€€É•ÍÕ±Ð(€€€€€€€€€€€™½È¥Ñ•´¥¸½µÁ…Ð(€€€€€€€€€€€¥˜¥Ñ•µl‰Ñ½½±}¹…µ”‰t€ôô€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆ(€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸¥Ñ•µl‰½ÕÑÁÕÐ‰t¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¤(€€€€€€€ulèÕt(€€€€€€€½µÁ…É¥Í½¹Ì€ôl(€€€€€€€€€€€¥Ñ•µl‰½ÕÑÁÕÐ‰t™½È¥Ñ•´¥¸½µÁ…Ð¥˜¥Ñ•µl‰Ñ½½±}¹…µ”‰t€ôô€‰½µÁ…É•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ìˆ(€€€€€€€t(€€€€€€€…•ÍÍ¥½¹Ì€ô±¥ÍÐ (€€€€€€€€€€€‘¥Ð¹™É½µ­•åÌ (€€€€€€€€€€€€€€€É•ÍÕ±Ð¹•Ð ‰…•ÍÍ¥½¸ˆ¤(€€€€€€€€€€€€€€€™½ÈÍ•…É ¥¸Í•…É¡•Ì(€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸Í•…É ¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¤(€€€€€€€€€€€€€€€¥˜É•ÍÕ±Ð¹•Ð ‰…•ÍÍ¥½¸ˆ¤(€€€€€€€€€€€€¤(€€€€€€€€¥lèÕt(€€€€€€€•Ù¥‘•¹”€ô±¥ÍÐ (€€€€€€€€€€€‘¥Ð¹™É½µ­•åÌ (€€€€€€€€€€€€€€€É•™•É•¹”(€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸l©Ù…±¥‘…Ñ¥½¹Ì°€©¥¹ÍÁ•Ñ¥½¹Ít(€€€€€€€€€€€€€€€™½ÈÉ•™•É•¹”¥¸É•ÍÕ±Ð¹•Ð ‰•Ù¥‘•¹•}É•™•É•¹•Ìˆ°mt¤(€€€€€€€€€€€€¤(€€€€€€€€¥lèÌÁt(€€€€€€€Õ¹É•Í½±Ù•€ô±¥ÍÐ (€€€€€€€€€€€‘¥Ð¹™É½µ­•åÌ (€€€€€€€€€€€€€€€Õ¹•ÉÑ…¥¹Ñä(€€€€€€€€€€€€€€€™½ÈÉ•ÍÕ±Ð¥¸¥¹ÍÁ•Ñ¥½¹Ì(€€€€€€€€€€€€€€€™½ÈÕ¹•ÉÑ…¥¹Ñä¥¸É•ÍÕ±Ð¹•Ð ‰•áÁ±¥¥Ñ}Õ¹•ÉÑ…¥¹Ñ¥•Ìˆ°mt¤(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡Õ¹•ÉÑ…¥¹Ñä°ÍÑÈ¤…¹Õ¹•ÉÑ…¥¹Ñä(€€€€€€€€€€€€¤(€€€€€€€€¥lèÈÁt(€€€€€€€¥˜…•ÍÍ¥½¹Ì…¹¹½Ð¥¹ÍÁ•Ñ¥½¹Ìè(€€€€€€€€€€€Õ¹É•Í½±Ù•€ôl(€€€€€€€€€€€€€€€€‰Y•É¥™äÑÉ•…Ñµ•¹Ð…¹µ…Ñ¡•µ½¹ÑÉ½°‘•Í¥¸¸ˆ°(€€€€€€€€€€€€€€€€‰ÍÍ•ÍÌ‘½Í”°Ñ¥µ”°É•Á±¥…Ñ”°…¹‰¥½±½¥…°µ½¹Ñ•áÐÍÕ¥Ñ…‰¥±¥Ñä¸ˆ°(€€€€€€€€€€€t(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}½…°ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‰¥½±½¥…±}½…°ˆ¤°(€€€€€€€€€€€€‰Í•…É¡}ÍÑÉ…Ñ•¥•Í}…±É•…‘å}•á•ÕÑ•ˆèÍ•…É¡•Íl´Ðét°(€€€€€€€€€€€€‰…•ÍÍ¥½¹Í}™½Õ¹ˆè…•ÍÍ¥½¹Ì°(€€€€€€€€€€€€‰Ù…±¥‘…Ñ¥½¹}ÍÑ…ÑÕÍ•ÌˆèÙ…±¥‘…Ñ¥½¹Ì°(€€€€€€€€€€€€‰¥¹ÍÁ•Ñ•‘}…¹‘¥‘…Ñ•}ÍÕµµ…É¥•Ìˆè¥¹ÍÁ•Ñ¥½¹Ì°(€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}½µÁ…É¥Í½¸ˆè½µÁ…É¥Í½¹Íl´Åt¥˜½µÁ…É¥Í½¹Ì•±Í”9½¹”°(€€€€€€€€€€€€‰É•µ…¥¹¥¹}Õ¹É•Í½±Ù•‘}ÅÕ•ÍÑ¥½¹ÌˆèÕ¹É•Í½±Ù•°(€€€€€€€€€€€€‰Ñ½½±}‰Õ‘•Ñ}É•µ…¥¹¥¹œˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰Ñ½½±}‰Õ‘•Ñ}É•µ…¥¹¥¹œˆ¤°(€€€€€€€€€€€€‰•Ù¥‘•¹•}É•™•É•¹•Ìˆè•Ù¥‘•¹”°(€€€€€€€ô((€€€‘•˜•ÍÑ¥µ…Ñ•}½¹Ñ•áÑ}½µÁ½¹•¹ÑÌ (€€€€€€€Í•±˜°É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°¡¥ÍÑ½Éäè±¥ÍÑm‘¥Ñt(€€€€¤€´ø‘¥ÑmÍÑÈ°¥¹Ñtè(€€€€€€€€ˆˆ‰ÍÑ¥µ…Ñ”Í…™”ÁÉ½µÁÐ½µÁ½¹•¹ÑÌÝ¥Ñ¡½ÕÐÍÑ½É¥¹œÁÉ½µÁÐÑ•áÐ½ÈÉ•…Í½¹¥¹œ¸ˆˆˆ((€€€€€€€‘•˜•ÍÑ¥µ…Ñ”¡Ù…±Õ”è¹ä¤€´ø¥¹Ðè(€€€€€€€€€€€É•ÑÕÉ¸µ…à Ä°€¡±•¸¡…¹½¹¥…±}©Í½¸¡Ù…±Õ”¤¤€¬€Ì¤€¼¼€Ð¤((€€€€€€€Ñ½½±}Í¡•µ…Ì€ôl(€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆèÍ•±˜¹Ñ½½±Ì¹•Ð¡¹…µ”¤¹‘•™¥¹¥Ñ¥½¸¹‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€€€€€€€€€€‰¥¹ÁÕÑ}Í¡•µ„ˆèÍ•±˜¹Ñ½½±Ì¹•Ð¡¹…µ”¤¹¥¹ÁÕÑ}µ½‘•°¹µ½‘•±}©Í½¹}Í¡•µ„ ¤°(€€€€€€€€€€€ô(€€€€€€€€€€€™½È¹…µ”¥¸É•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ì(€€€€€€€t(€€€€€€€ÍÑ…Ñ•}ÍÕµµ…Éä€ôÍ•±˜¹}ÍÑ…Ñ•}ÍÕµµ…Éä¡É•ÅÕ•ÍÐ°¡¥ÍÑ½Éä¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍåÍÑ•µ}¥¹ÍÑÉÕÑ¥½¹Ìˆè•ÍÑ¥µ…Ñ”¡É•ÅÕ•ÍÐ¹¥¹ÍÑÉÕÑ¥½¹Ì¤°(€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}‘•™¥¹¥Ñ¥½¸ˆè•ÍÑ¥µ…Ñ” (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}¹…µ”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰•¹‘Á½¥¹Ñ}¹…µ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€‰‰¥½±½¥…±}½…°ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‰¥½±½¥…±}½…°ˆ¤°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰•áÁ½Í•‘}Ñ½½±}Í¡•µ…Ìˆè•ÍÑ¥µ…Ñ”¡Ñ½½±}Í¡•µ…Ì¤°(€€€€€€€€€€€€‰½¹Ù•ÉÍ…Ñ¥½¹}¡¥ÍÑ½Éäˆè•ÍÑ¥µ…Ñ” (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰¹½Éµ…±}ÑÕÉ¹Í}½µÁ±•Ñ•ˆè±•¸¡¡¥ÍÑ½Éä¤°(€€€€€€€€€€€€€€€€€€€€‰‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…•Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´¹•Ð ‰‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”ˆ¤™½È¥Ñ•´¥¸¡¥ÍÑ½Éål´àét(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰Ñ½½±}É•ÍÕ±ÑÌˆè•ÍÑ¥µ…Ñ”¡ÍÑ…Ñ•}ÍÕµµ…Éä¤°(€€€€€€€€€€€€‰ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕÑ}Í¡•µ„ˆè•ÍÑ¥µ…Ñ” (€€€€€€€€€€€€€€€É•Í½±Ù•}½ÕÑÁÕÑ}Í¡•µ„¡É•ÅÕ•ÍÐ¹½ÕÑÁÕÑ}Í¡•µ…}¹…µ”¤¹µ½‘•±}©Í½¹}Í¡•µ„ ¤(€€€€€€€€€€€€¤°(€€€€€€€ô((€€€‘•˜}ÕÍ…”¡Í•±˜°É•ÍÕ±Ðè¹ä¤€´øUÍ…•I•Á½ÉÐè(€€€€€€€É…Ü€ô•Ñ…ÑÑÈ¡•Ñ…ÑÑÈ¡É•ÍÕ±Ð°€‰½¹Ñ•áÑ}ÝÉ…ÁÁ•Èˆ°9½¹”¤°€‰ÕÍ…”ˆ°9½¹”¤(€€€€€€€É•ÍÁ½¹Í•Ì€ô±¥ÍÐ¡•Ñ…ÑÑÈ¡É•ÍÕ±Ð°€‰É…Ý}É•ÍÁ½¹Í•Ìˆ°9½¹”¤½Èmt¤(€€€€€€€¥˜É…Ü¥Ì9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}ÕÍ…•}™É½µ}É…Ý}É•ÍÁ½¹Í•Ì¡É•ÍÁ½¹Í•Ì¤(€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ì€ô¥¹Ð¡•Ñ…ÑÑÈ¡É…Ü°€‰¥¹ÁÕÑ}Ñ½­•¹Ìˆ°€À¤½È€À¤(€€€€€€€½ÕÑÁÕÑ}Ñ½­•¹Ì€ô¥¹Ð¡•Ñ…ÑÑÈ¡É…Ü°€‰½ÕÑÁÕÑ}Ñ½­•¹Ìˆ°€À¤½È€À¤(€€€€€€€…¡•‘}Ñ½­•¹Ì€ô¥¹Ð (€€€€€€€€€€€•Ñ…ÑÑÈ¡•Ñ…ÑÑÈ¡É…Ü°€‰¥¹ÁÕÑ}Ñ½­•¹Í}‘•Ñ…¥±Ìˆ°9½¹”¤°€‰…¡•‘}Ñ½­•¹Ìˆ°€À¤½È€À(€€€€€€€€¤(€€€€€€€½ÍÑ}ÕÍ€ô€ (€€€€€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ì€¨Í•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹¥¹ÁÕÑ}½ÍÑ}Á•É}µ¥±±¥½¹}ÕÍ(€€€€€€€€€€€€¬½ÕÑÁÕÑ}Ñ½­•¹Ì€¨Í•±˜¹½¹™¥ÕÉ…Ñ¥½¸¹½ÕÑÁÕÑ}½ÍÑ}Á•É}µ¥±±¥½¹}ÕÍ(€€€€€€€€¤€¼€Å|ÀÀÁ|ÀÀÀ(€€€€€€€É•ÑÕÉ¸UÍ…•I•Á½ÉÐ (€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌô‰ÕÍ…•}É•½É‘•ˆ°(€€€€€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ìõ¥¹ÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€½ÕÑÁÕÑ}Ñ½­•¹Ìõ½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€…¡•‘}Ñ½­•¹Ìõ…¡•‘}Ñ½­•¹Ì°(€€€€€€€€€€€½ÍÑ}•¹ÑÌõ½ÍÑ}ÕÍ€¨€ÄÀÀ°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘ÌõÍ•±˜¹}Í…™•}¥‘Ì¡É•ÍÁ½¹Í•Ì°€‰É•ÅÕ•ÍÑ}¥ˆ¤°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘ÌõÍ•±˜¹}Í…™•}¥‘Ì¡É•ÍÁ½¹Í•Ì°€‰É•ÍÁ½¹Í•}¥ˆ¤°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ìõµ…à¡¥¹Ð¡•Ñ…ÑÑÈ¡É…Ü°€‰É•ÅÕ•ÍÑÌˆ°€À¤½È€À¤°±•¸¡É•ÍÁ½¹Í•Ì¤¤°(€€€€€€€€¤(