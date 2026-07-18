"""Provider-neutral contract and deterministic Phase-0 fake provider."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .contracts import (
    AgentRunRequest,
    NormalizedAgentError,
    ProviderToolRequest,
    ProviderTurn,
    UsageReport,
)


class ProviderFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ProviderTimeout(TimeoutError):
    pass


class AgentProvider(Protocol):
    name: str

    def run_turn(
        self,
        request: AgentRunRequest,
        history: list[dict],
        *,
        interruption_requested: bool,
    ) -> ProviderTurn: ...


ProviderFactory = Callable[[], AgentProvider]


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if name in self._providers:
            raise ValueError(f"provider {name!r} is already registered")
        self._providers[name] = factory

    def create(self, name: str) -> AgentProvider:
        try:
            return self._providers[name]()
        except KeyError as exc:
            raise ProviderFailure(f"Provider {name!r} is not configured.", retryable=False) from exc

    def configured(self) -> list[str]:
        return sorted(self._providers)


class FakeAgentProvider:
    """Stateless deterministic provider used for tests and prepared Phase-0 demos."""

    name = "fake"

    def __init__(self, script: list[ProviderTurn] | None = None):
        self.script = script
        self._transient_failures = 0

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
                    safe_message="Agent run was interrupted by the orchestrator.",
                    retryable=False,
                    category="interruption",
                ),
            )
        mode = str(request.context.get("fake_mode", "discovery"))
        if mode == "timeout":
            raise ProviderTimeout("Prepared fake-provider timeout.")
        if mode == "failure":
            raise ProviderFailure("Prepared fake-provider failure.", retryable=False)
        if mode == "transient_failure" and self._transient_failures == 0:
            self._transient_failures += 1
            raise ProviderFailure("Prepared transient provider failure.", retryable=True)
        if self.script is not None:
            index = len(history)
            if index >= len(self.script):
                raise ProviderFailure("Fake-provider script was exhausted.", retryable=False)
            return self.script[index]
        if mode == "malformed":
            return ProviderTurn(
                kind="output",
                output={"unexpected": True},
                usage=self._usage(len(history)),
            )
        if mode == "approval":
            approval = request.context.get("approval")
            if not isinstance(approval, dict):
                raise ProviderFailure("Prepared approval payload is missing.", retryable=False)
            return ProviderTurn(kind="approval", approval=approval, usage=self._usage(len(history)))
        return self._discovery_turn(request, history)

    def _discovery_turn(self, request: AgentRunRequest, history: list[dict]) -> ProviderTurn:
        sequence = [
            ProviderToolRequest(
                tool_name="inspect_endpoint_registry",
                arguments={"repository_root": "."},
                idempotency_key="discovery-registry-v1",
            ),
            ProviderToolRequest(
                tool_name="list_known_source_adapters",
                arguments={"repository_root": "."},
                idempotency_key="discovery-adapters-v1",
            ),
            ProviderToolRequest(
                tool_name="summarize_existing_endpoint_pipeline",
                arguments={"repository_root": ".", "endpoint_ids": ["ER", "AR"]},
                idempotency_key="discovery-pipelines-v1",
            ),
            ProviderToolRequest(
                tool_name="create_dataset_candidate_artifact",
                arguments={
                    "endpoint_name": request.context.get("endpoint_name", "Oxidative stress")
                },
                idempotency_key="discovery-prepared-candidates-v1",
            ),
        ]
        if len(history) < len(sequence):
            return ProviderTurn(
                kind="tool",
                tool_request=sequence[len(history)],
                usage=self._usage(len(history)),
            )
        endpoint_name = str(request.context.get("endpoint_name", "Oxidative stress"))
        fixture_path = Path(__file__).with_name("fixtures") / "phase1_oxidative_stress_replay.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        output = fixture["output"]
        output["endpoint_name"] = endpoint_name
        output["endpoint_definition_summary"] = str(request.context.get("biological_goal", ""))
        for index, candidate in enumerate(output["candidates"], start=1):
            candidate["title"] = (
                f"Prepared metadata candidate {chr(64 + index)} for {endpoint_name}"
            )
        return ProviderTurn(
            kind="output",
            output=output,
            usage=self._usage(len(history)),
        )

    @staticmethod
    def _usage(index: int) -> UsageReport:
        return UsageReport(
            input_tokens=120 + index * 10,
            output_tokens=40 + index * 5,
            cached_tokens=0,
            cost_cents=0.0,
            provider_request_ids=[f"fake-request-{index + 1}"],
        )


class SlowFakeAgentProvider(FakeAgentProvider):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay_seconds = delay_seconds

    def run_turn(self, *args, **kwargs) -> ProviderTurn:
        time.sleep(self.delay_seconds)
        return super().run_turn(*args, **kwargs)
