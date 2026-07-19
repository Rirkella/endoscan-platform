"""Environment-backed Phase-1 agent configuration without secret disclosure."""

from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AgentRunMode(str, Enum):
    LIVE = "live"
    CACHED = "cached"
    REPLAY = "replay"


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "fake"
    model: str = "gpt-5.4-mini"
    planner_provider: str = "fake"
    planner_model: str = "gpt-5.4-mini"
    worker_provider: str = "fake"
    worker_model: str = "gpt-5.4-mini"
    run_mode: AgentRunMode = AgentRunMode.REPLAY
    benchmark_mode: str = "standard_training_dataset_discovery"
    api_key: SecretStr | None = None
    maximum_turns: int = Field(default=6, ge=1, le=32)
    maximum_tool_calls: int = Field(default=8, ge=1, le=64)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    maximum_input_tokens: int = Field(default=20_000, ge=1)
    maximum_output_tokens: int = Field(default=2_500, ge=1)
    maximum_cost_usd: float = Field(default=0.20, ge=0, le=100)
    retry_count: int = Field(default=0, ge=0, le=5)
    global_maximum_input_tokens: int = Field(default=80_000, ge=1)
    global_maximum_output_tokens: int = Field(default=12_000, ge=1)
    global_maximum_tool_calls: int = Field(default=48, ge=0, le=512)
    global_maximum_cost_usd: float = Field(default=1.00, ge=0, le=500)
    global_timeout_seconds: float = Field(default=900.0, gt=0, le=86_400)
    maximum_gap_discovery_rounds: int = Field(default=2, ge=0, le=10)
    input_cost_per_million_usd: float = Field(default=0.75, ge=0)
    output_cost_per_million_usd: float = Field(default=4.50, ge=0)
    tracing_enabled: bool = False
    source_cache_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    source_request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    source_response_maximum_bytes: int = Field(default=1_500_000, ge=1_024, le=5_000_000)
    source_requests_per_second: float = Field(default=2.5, gt=0, le=10)
    ncbi_email: str | None = None
    ncbi_api_key: SecretStr | None = None

    @field_validator("provider", "planner_provider", "worker_provider")
    @classmethod
    def supported_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"fake", "openai"}:
            raise ValueError("ENDOSCAN_AGENT_PROVIDER must be fake or openai")
        return normalized

    @classmethod
    def from_env(cls) -> AgentConfiguration:
        provider = os.environ.get("ENDOSCAN_AGENT_PROVIDER", "fake").strip().lower()
        key = os.environ.get("OPENAI_API_KEY") or None
        model = os.environ.get("ENDOSCAN_AGENT_MODEL", "gpt-5.4-mini").strip()
        planner_provider = os.environ.get("ENDOSCAN_PLANNER_PROVIDER", provider).strip().lower()
        planner_model = os.environ.get("ENDOSCAN_PLANNER_MODEL", model).strip()
        worker_provider = os.environ.get("ENDOSCAN_WORKER_PROVIDER", provider).strip().lower()
        worker_model = os.environ.get("ENDOSCAN_WORKER_MODEL", model).strip()
        openai_role_configured = "openai" in {provider, planner_provider, worker_provider}
        requested_mode = os.environ.get("ENDOSCAN_AGENT_MODE")
        if requested_mode:
            mode = requested_mode.strip().lower()
        else:
            mode = "live" if openai_role_configured and key else "replay"
        if mode in {"live", "cached"} and (not openai_role_configured or not key):
            mode = "replay"
        default_input_rate = "0.75" if model == "gpt-5.4-mini" else "0"
        default_output_rate = "4.50" if model == "gpt-5.4-mini" else "0"
        return cls(
            provider=provider,
            model=model,
            planner_provider=planner_provider,
            planner_model=planner_model,
            worker_provider=worker_provider,
            worker_model=worker_model,
            run_mode=AgentRunMode(mode),
            benchmark_mode=os.environ.get(
                "ENDOSCAN_BENCHMARK_MODE", "standard_training_dataset_discovery"
            ).strip(),
            api_key=SecretStr(key) if key else None,
            maximum_turns=int(os.environ.get("ENDOSCAN_AGENT_MAX_TURNS", "6")),
            maximum_tool_calls=int(os.environ.get("ENDOSCAN_AGENT_MAX_TOOL_CALLS", "8")),
            timeout_seconds=float(os.environ.get("ENDOSCAN_AGENT_TIMEOUT_SECONDS", "120")),
            maximum_input_tokens=int(os.environ.get("ENDOSCAN_AGENT_MAX_INPUT_TOKENS", "20000")),
            maximum_output_tokens=int(os.environ.get("ENDOSCAN_AGENT_MAX_OUTPUT_TOKENS", "2500")),
            maximum_cost_usd=float(os.environ.get("ENDOSCAN_AGENT_MAX_COST_USD", "0.20")),
            retry_count=int(os.environ.get("ENDOSCAN_AGENT_RETRY_COUNT", "0")),
            global_maximum_input_tokens=int(
                os.environ.get("ENDOSCAN_WORKFLOW_MAX_INPUT_TOKENS", "80000")
            ),
            global_maximum_output_tokens=int(
                os.environ.get("ENDOSCAN_WORKFLOW_MAX_OUTPUT_TOKENS", "12000")
            ),
            global_maximum_tool_calls=int(os.environ.get("ENDOSCAN_WORKFLOW_MAX_TOOL_CALLS", "48")),
            global_maximum_cost_usd=float(os.environ.get("ENDOSCAN_WORKFLOW_MAX_COST_USD", "1.00")),
            global_timeout_seconds=float(
                os.environ.get("ENDOSCAN_WORKFLOW_TIMEOUT_SECONDS", "900")
            ),
            maximum_gap_discovery_rounds=int(
                os.environ.get("ENDOSCAN_MAX_GAP_DISCOVERY_ROUNDS", "2")
            ),
            input_cost_per_million_usd=float(
                os.environ.get("ENDOSCAN_AGENT_INPUT_COST_PER_1M_USD", default_input_rate)
            ),
            output_cost_per_million_usd=float(
                os.environ.get("ENDOSCAN_AGENT_OUTPUT_COST_PER_1M_USD", default_output_rate)
            ),
            tracing_enabled=_bool_env("ENDOSCAN_OPENAI_TRACING", False),
            source_cache_ttl_seconds=int(
                os.environ.get("ENDOSCAN_SOURCE_CACHE_TTL_SECONDS", "86400")
            ),
            source_request_timeout_seconds=float(
                os.environ.get("ENDOSCAN_SOURCE_TIMEOUT_SECONDS", "15")
            ),
            source_response_maximum_bytes=int(
                os.environ.get("ENDOSCAN_SOURCE_MAX_RESPONSE_BYTES", "1500000")
            ),
            source_requests_per_second=float(
                os.environ.get("ENDOSCAN_SOURCE_REQUESTS_PER_SECOND", "2.5")
            ),
            ncbi_email=(os.environ.get("NCBI_EMAIL") or "").strip() or None,
            ncbi_api_key=(
                SecretStr(os.environ["NCBI_API_KEY"]) if os.environ.get("NCBI_API_KEY") else None
            ),
        )

    @property
    def api_key_present(self) -> bool:
        return self.api_key is not None

    @property
    def live_enabled(self) -> bool:
        return (
            "openai"
            in {
                self.provider,
                self.planner_provider,
                self.worker_provider,
            }
            and self.api_key_present
        )

    def controlled_source_discovery(self) -> AgentConfiguration:
        """Return the stricter first-workflow budget without weakening global configuration."""

        return self.model_copy(
            update={
                "maximum_turns": min(self.maximum_turns, 6),
                "maximum_tool_calls": min(self.maximum_tool_calls, 6),
                "timeout_seconds": min(self.timeout_seconds, 120.0),
                "maximum_input_tokens": min(self.maximum_input_tokens, 8_000),
                "maximum_output_tokens": min(self.maximum_output_tokens, 1_500),
                "maximum_cost_usd": min(self.maximum_cost_usd, 0.20),
                "retry_count": 0,
                "global_maximum_input_tokens": min(self.global_maximum_input_tokens, 32_000),
                "global_maximum_output_tokens": min(self.global_maximum_output_tokens, 6_000),
                "global_maximum_tool_calls": min(self.global_maximum_tool_calls, 24),
                "global_maximum_cost_usd": min(self.global_maximum_cost_usd, 0.80),
                "global_timeout_seconds": min(self.global_timeout_seconds, 600.0),
                "maximum_gap_discovery_rounds": 0,
            }
        )

    def public_status(self) -> dict[str, object]:
        controlled = self.controlled_source_discovery()
        return {
            "schema_version": "1.0.0",
            "provider": self.provider,
            "model": self.model,
            "planner_provider": self.planner_provider,
            "planner_model": self.planner_model,
            "worker_provider": self.worker_provider,
            "worker_model": self.worker_model,
            "run_mode": self.run_mode.value,
            "benchmark_mode": self.benchmark_mode,
            "api_key_present": self.api_key_present,
            "live_mode_enabled": self.live_enabled,
            "source_tools_available": True,
            "tracing_enabled": self.tracing_enabled,
            "configured_budget": {
                "maximum_turns": self.maximum_turns,
                "maximum_tool_calls": self.maximum_tool_calls,
                "timeout_seconds": self.timeout_seconds,
                "maximum_input_tokens": self.maximum_input_tokens,
                "maximum_output_tokens": self.maximum_output_tokens,
                "maximum_cost_usd": self.maximum_cost_usd,
                "retry_count": self.retry_count,
                "input_cost_per_million_usd": self.input_cost_per_million_usd,
                "output_cost_per_million_usd": self.output_cost_per_million_usd,
            },
            "global_workflow_budget": {
                "maximum_input_tokens": self.global_maximum_input_tokens,
                "maximum_output_tokens": self.global_maximum_output_tokens,
                "maximum_tool_calls": self.global_maximum_tool_calls,
                "maximum_cost_usd": self.global_maximum_cost_usd,
                "maximum_gap_discovery_rounds": self.maximum_gap_discovery_rounds,
                "timeout_seconds": self.global_timeout_seconds,
                "provider_retries": self.retry_count,
            },
            "controlled_source_discovery_budget": {
                "maximum_agent_runs": 4,
                "maximum_turns_per_agent": controlled.maximum_turns,
                "maximum_tool_calls_per_agent": controlled.maximum_tool_calls,
                "maximum_total_tool_calls": controlled.global_maximum_tool_calls,
                "maximum_input_tokens_per_agent": controlled.maximum_input_tokens,
                "maximum_output_tokens_per_agent": controlled.maximum_output_tokens,
                "maximum_cost_per_agent_usd": controlled.maximum_cost_usd,
                "maximum_total_cost_usd": controlled.global_maximum_cost_usd,
                "per_agent_timeout_seconds": controlled.timeout_seconds,
                "global_timeout_seconds": controlled.global_timeout_seconds,
                "provider_retries": controlled.retry_count,
                "source_retries": 0,
                "maximum_gap_discovery_rounds": controlled.maximum_gap_discovery_rounds,
            },
        }


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
