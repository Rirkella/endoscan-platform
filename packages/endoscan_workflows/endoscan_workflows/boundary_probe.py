"""Development-only, zero-network probe for the exact production adapter boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agents.exceptions import UserError
from agents.models.interface import Model, ModelProvider

from .config import AgentConfiguration, AgentRunMode
from .contracts import AdapterBoundaryProbeResult
from .discovery import DISCOVERY_TOOLS, DiscoveryOutput, discovery_request
from .openai_provider import OpenAIAgentProvider
from .providers import sanitize_local_sdk_message
from .tools import ToolRegistry


class _ModelCallBoundaryReached(RuntimeError):
    pass


class _BoundaryModel(Model):
    async def get_response(self, *args: Any, **kwargs: Any):
        raise _ModelCallBoundaryReached

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            raise _ModelCallBoundaryReached
            yield  # pragma: no cover - makes this an async iterator

        return stream()


class _BoundaryModelProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        return _BoundaryModel()


class AdapterBoundaryProbe:
    """Validate production SDK construction and stop at the first model call."""

    def __init__(self, configuration: AgentConfiguration, tools: ToolRegistry):
        self.configuration = configuration
        self.tools = tools

    def check(self) -> AdapterBoundaryProbeResult:
        request = discovery_request(
            workflow_id="boundary-probe-workflow",
            step_id="boundary-probe-step",
            endpoint_name="Oxidative stress",
            biological_goal="Validate local adapter construction without external requests.",
            configuration=self.configuration.model_copy(
                update={"provider": "openai", "run_mode": AgentRunMode.LIVE}
            ),
        )
        adapter = OpenAIAgentProvider(self.configuration, self.tools)
        try:
            agent = adapter._build_agent(request)
            run_config = adapter._build_run_config(request, model_provider=_BoundaryModelProvider())
            adapter.runner(
                agent,
                adapter._turn_input(request, []),
                max_turns=1,
                run_config=run_config,
            )
        except _ModelCallBoundaryReached:
            return self._result(valid=True, reached=True)
        except UserError as exc:
            return self._result(
                valid=False,
                reached=False,
                developer_message=sanitize_local_sdk_message(exc.message),
                exception_class=type(exc).__name__,
            )
        except Exception as exc:  # local probe must fail closed without raw exception text
            return self._result(
                valid=False,
                reached=False,
                exception_class=type(exc).__name__,
            )
        return self._result(valid=False, reached=False, exception_class="BoundaryNotReached")

    def _result(
        self,
        *,
        valid: bool,
        reached: bool,
        developer_message: str | None = None,
        exception_class: str | None = None,
    ) -> AdapterBoundaryProbeResult:
        return AdapterBoundaryProbeResult(
            local_sdk_configuration_valid=valid,
            model_call_boundary_reached=reached,
            developer_message=developer_message,
            exception_class=exception_class,
            sdk_version=OpenAIAgentProvider.sdk_version(),
            model=self.configuration.model,
            tool_count=len(DISCOVERY_TOOLS),
            output_schema_name=DiscoveryOutput.__name__,
            network_requests=0,
        )
