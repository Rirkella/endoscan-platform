"""Explicit test-only bounded tools for harness contract and failure-path tests."""

from __future__ import annotations

import time

from .contracts import (
    IdempotencyClassification,
    SideEffectClassification,
    ToolDefinition,
    WorkflowState,
)
from .tools import EchoInput, EchoOutput, RegisteredTool, ToolRegistry


def offline_test_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def add(name, implementation, *, stage=WorkflowState.DISCOVERING_DATA, timeout=0.1):
        registry.register(
            RegisteredTool(
                ToolDefinition(
                    name=name,
                    description=f"Test-only {name} tool.",
                    input_schema_name=EchoInput.__name__,
                    output_schema_name=EchoOutput.__name__,
                    required_permissions=["test:invoke"],
                    side_effect=SideEffectClassification.NONE,
                    idempotency=IdempotencyClassification.IDEMPOTENT_WITH_KEY,
                    timeout_seconds=timeout,
                    allowed_workflow_stages=[stage],
                    implementation_version="test-v1",
                ),
                EchoInput,
                EchoOutput,
                implementation,
            )
        )

    add("test_success", lambda request: EchoOutput(value=request.value))
    add("test_output_validation_failure", lambda _request: {"unexpected": True})
    add("test_timeout", lambda request: (time.sleep(0.2), EchoOutput(value=request.value))[1])
    add(
        "test_prohibited",
        lambda request: EchoOutput(value=request.value),
        stage=WorkflowState.TRAINING,
    )
    add("test_idempotent_retry", lambda request: EchoOutput(value=request.value))
    return registry
