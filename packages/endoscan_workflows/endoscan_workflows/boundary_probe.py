"""Development-only, zero-network probe for the exact production adapter boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agents.exceptions import UserError
from agents.models.interface import Model, ModelProvider

from .config import AgentConfiguration, AgentRunMode
from .contracts import AdapterBoundaryProbeResult, WorkflowState
from .discovery import DISCOVERY_STAGE_TOOLS, DiscoveryOutput, discovery_request
from .openai_provider import OpenAIAgentProvider, sdk_output_schema
from .providers import sanitize_local_sdk_message
from .repository import canonical_json
from .tools import ToolRegistry
from .training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    SPECIALIZED_AGENT_SEQUENCE,
    BlindBenchmarkInitialContext,
    specialized_agent_request,
)

SPECIALIZED_AGENT_STAGES = {
    "Dataset Specification Review Agent": WorkflowState.REVIEWING_DATASET_SPECIFICATION,
    "Activity Evidence Discovery Agent": WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
    "Transcriptomic Evidence Discovery Agent": WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
    "Chemical Identity and Structure Source Discovery Agent": (
        WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES
    ),
    "Supporting Metadata Discovery Agent": WorkflowState.DISCOVERING_SUPPORTING_METADATA,
    "Training Dataset Assembly Strategy Planner": WorkflowState.PLANNING_ASSEMBLY_STRATEGIES,
    "Assembly Strategy Evaluation Agent": WorkflowState.COMPARING_ASSEMBLY_STRATEGIES,
}


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
        request = request.model_copy(
            update={
                "available_tools": DISCOVERY_STAGE_TOOLS["search_planning"],
                "context": {
                    **request.context,
                    "discovery_substage": "search_planning",
                    "tools_exposed": DISCOVERY_STAGE_TOOLS["search_planning"],
                },
            }
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
            tool_count=len(DISCOVERY_STAGE_TOOLS["search_planning"]),
            output_schema_name=DiscoveryOutput.__name__,
            network_requests=0,
        )

    def check_specialized_agents(self) -> list[dict[str, Any]]:
        """Reach, but never cross, the model-call boundary for every agent role."""

        results: list[dict[str, Any]] = []
        adapter = OpenAIAgentProvider(self.configuration, self.tools)
        initial_context = BlindBenchmarkInitialContext(
            endpoint_name="Boundary probe endpoint",
            biological_goal="Validate local construction with zero external network requests.",
            benchmark_mode=BLIND_TRAINING_DATASET_DISCOVERY,
            target_training_dataset_contract={"contract_version": "1.0.0"},
            source_adapter_capabilities=[],
            approved_scientific_policies=[],
            allowed_tools=[],
            planner_provider=self.configuration.planner_provider,
            planner_model=self.configuration.planner_model,
            worker_provider=self.configuration.worker_provider,
            worker_model=self.configuration.worker_model,
            budgets={"retry_count": self.configuration.retry_count},
        )
        for definition in SPECIALIZED_AGENT_SEQUENCE:
            is_planner = definition.role == "planner"
            model = (
                self.configuration.planner_model if is_planner else self.configuration.worker_model
            )
            request = specialized_agent_request(
                definition=definition,
                workflow_id="specialized-boundary-probe",
                step_id=f"probe-{len(results) + 1}",
                workflow_stage=SPECIALIZED_AGENT_STAGES[definition.agent_name],
                initial_context=initial_context,
                validated_artifacts={name: {} for name in definition.receives_artifacts},
                configuration=self.configuration,
            )
            valid = False
            reached = False
            exception_class = None
            developer_message = None
            handlers: dict[str, Any] = {}
            fingerprint = None
            try:
                agent = adapter._build_agent(request)
                fingerprint = adapter.structured_output_fingerprint(request)
                run_config = adapter._build_run_config(
                    request, model_provider=_BoundaryModelProvider()
                )
                capture: dict[str, Any] = {}
                handlers = adapter._error_handlers(request, capture, 0.0)
                runner_kwargs: dict[str, Any] = {
                    "max_turns": 1,
                    "run_config": run_config,
                }
                if handlers:
                    runner_kwargs["error_handlers"] = handlers
                adapter.runner(
                    agent,
                    adapter._turn_input(request, []),
                    **runner_kwargs,
                )
            except _ModelCallBoundaryReached:
                valid = True
                reached = True
            except UserError as exc:
                exception_class = type(exc).__name__
                developer_message = sanitize_local_sdk_message(exc.message)
            except Exception as exc:
                exception_class = type(exc).__name__
                fingerprint = None
            results.append(
                {
                    "agent_name": definition.agent_name,
                    "role": definition.role,
                    "local_sdk_configuration_valid": valid,
                    "model_call_boundary_reached": reached,
                    "developer_message": developer_message,
                    "exception_class": exception_class,
                    "sdk_version": OpenAIAgentProvider.sdk_version(),
                    "provider": request.model.provider,
                    "model": model,
                    "tool_count": len(definition.allowed_tools),
                    "tools": definition.allowed_tools,
                    "output_schema_name": definition.output_schema_name,
                    "output_schema_version": request.agent_version,
                    "output_schema_size": len(
                        canonical_json(
                            sdk_output_schema(definition.output_schema_name).json_schema()
                        )
                    ),
                    "output_type_present": bool(fingerprint and fingerprint.output_type_present),
                    "strict_json_schema": bool(fingerprint and fingerprint.strict_json_schema),
                    "api_surface": fingerprint.api_surface if fingerprint else None,
                    "schema_hash": fingerprint.schema_hash if fingerprint else None,
                    "runtime_configuration_hash": (
                        fingerprint.runtime_configuration_hash if fingerprint else None
                    ),
                    "boundary_probe_configuration_hash": (
                        fingerprint.boundary_probe_configuration_hash if fingerprint else None
                    ),
                    "boundary_runtime_contracts_match": bool(
                        fingerprint and fingerprint.boundary_runtime_contracts_match
                    ),
                    "error_handler_names": sorted(handlers),
                    "semantic_hints_present": (
                        "endpoint_request_semantic_hints" in request.context["validated_artifacts"]
                    ),
                    "provider_retries": request.budget.retry_count,
                    "network_requests": 0,
                }
            )
        return results
