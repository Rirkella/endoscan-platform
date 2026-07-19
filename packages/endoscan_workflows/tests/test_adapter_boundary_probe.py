from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from agents import AgentOutputSchema, ModelResponse
from agents.models.interface import Model, ModelProvider
from agents.strict_schema import ensure_strict_json_schema
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall
from pydantic import SecretStr

from endoscan_workflows.boundary_probe import AdapterBoundaryProbe
from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.discovery import (
    DISCOVERY_STAGE_TOOLS,
    DISCOVERY_TOOLS,
    DiscoveryOutput,
    discovery_request,
)
from endoscan_workflows.openai_provider import TOOL_ENVELOPE, OpenAIAgentProvider
from endoscan_workflows.tools import phase1_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]


class OfflineDiscoveryService:
    def __getattr__(self, name: str):
        return lambda *args, **kwargs: SimpleNamespace()


def registry():
    return phase1_tool_registry(REPO_ROOT, OfflineDiscoveryService())


class ToolCallModel(Model):
    async def get_response(self, *args, **kwargs):
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments='{"accessions":["GSE12345"]}',
                    call_id="call-offline-tool",
                    name="validate_geo_accessions",
                    type="function_call",
                )
            ],
            usage=Usage(),
            response_id="response-offline-tool",
        )

    def stream_response(self, *args, **kwargs):
        raise AssertionError("The production adapter does not use streaming")


class ToolCallModelProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        return ToolCallModel()


def test_exact_production_adapter_reaches_model_boundary_without_network(monkeypatch) -> None:
    def external_client_forbidden(*args, **kwargs):
        raise AssertionError("The boundary probe must not construct an external client")

    monkeypatch.setattr("endoscan_workflows.openai_provider.AsyncOpenAI", external_client_forbidden)
    result = AdapterBoundaryProbe(AgentConfiguration(), registry()).check()
    assert result.local_sdk_configuration_valid is True
    assert result.model_call_boundary_reached is True
    assert result.network_requests == 0
    assert result.sdk_version == "0.18.2"
    assert result.tool_count == len(DISCOVERY_STAGE_TOOLS["search_planning"]) == 1
    assert result.output_schema_name == "DiscoveryOutput"
    assert result.developer_message is None


def test_production_agent_tools_and_output_schema_are_strict_sdk_inputs() -> None:
    tools = registry()
    configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("offline-placeholder"),
    )
    request = discovery_request(
        workflow_id="boundary-test-workflow",
        step_id="boundary-test-step",
        endpoint_name="Oxidative stress",
        biological_goal="Validate production SDK construction offline.",
        configuration=configuration,
    )
    adapter = OpenAIAgentProvider(configuration, tools)
    request = request.model_copy(
        update={"available_tools": DISCOVERY_STAGE_TOOLS["search_planning"]}
    )
    agent = adapter._build_agent(request)
    assert agent.instructions == request.instructions
    assert (
        "Use exact controlled-vocabulary values exposed by each tool schema" in agent.instructions
    )
    assert agent.model == "gpt-5.4-mini"
    assert agent.output_type is DiscoveryOutput
    assert agent.tool_use_behavior == "stop_on_first_tool"
    assert len(agent.tools) == 1
    for name in DISCOVERY_TOOLS:
        ensure_strict_json_schema(copy.deepcopy(tools.get(name).input_model.model_json_schema()))
    AgentOutputSchema(DiscoveryOutput)


def test_function_tool_proxy_uses_supported_signature_and_string_result() -> None:
    adapter = OpenAIAgentProvider(AgentConfiguration(), registry())
    tool = adapter._proxy_tool("validate_geo_accessions")
    raw = asyncio.run(tool.on_invoke_tool(None, '{"accessions":["GSE12345"]}'))
    payload = json.loads(raw)
    assert isinstance(raw, str)
    assert payload == {
        TOOL_ENVELOPE: True,
        "arguments": {"accessions": ["GSE12345"]},
        "tool_name": "validate_geo_accessions",
    }


def test_sdk_stop_on_first_tool_returns_proxy_envelope_to_endoscan_harness() -> None:
    configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("offline-placeholder"),
    )
    tools = registry()
    request = discovery_request(
        workflow_id="tool-proxy-workflow",
        step_id="tool-proxy-step",
        endpoint_name="Oxidative stress",
        biological_goal="Validate the supported tool-proxy run loop offline.",
        configuration=configuration,
    )
    adapter = OpenAIAgentProvider(configuration, tools)
    result = adapter.runner(
        adapter._build_agent(request),
        adapter._turn_input(request, []),
        max_turns=1,
        run_config=adapter._build_run_config(request, model_provider=ToolCallModelProvider()),
    )
    turn = adapter._translate_result(result)
    assert turn.kind == "tool"
    assert turn.tool_request.tool_name == "validate_geo_accessions"
    assert turn.tool_request.arguments == {"accessions": ["GSE12345"]}
