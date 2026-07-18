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
    run_mode: AgentRunMode = AgentRunMode.REPLAY
    api_key: SecretStr | None = None
    maximum_turns: int = Field(default=6, ge=1, le=32)
    maximum_tool_calls: int = Field(default=6, ge=1, le=64)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    maximum_input_tokens: int = Field(default=8_000, ge=1)
    maximum_output_tokens: int = Field(default=1_500, ge=1)
    maximum_cost_usd: float = Field(default=0.20, ge=0, le=100)
    retry_count: int = Field(default=0, ge=0, le=5)
    input_cost_per_million_usd: float = Field(default=0.75, ge=0)
    output_cost_per_million_usd: float = Field(default=4.50, ge=0)
    tracing_enabled: bool = False
    source_cache_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    source_request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    source_response_maximum_bytes: int = Field(default=1_500_000, ge=1_024, le=5_000_000)
    source_requests_per_second: float = Field(default=2.5, gt=0, le=10)
    ncbi_email: str | None = None
    ncbi_api_key: SecretStr | None = None

    @field_validator("provider")
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
        requested_mode = os.environ.get("ENDOSCAN_AGENT_MODE")
        if requested_mode:
            mode = requested_mode.strip().lower()
        else:
            mode = "live" if provider == "openai" and key else "replay"
        if mode in {"live", "cached"} and (provider != "openai" or not key):
            mode = "replay"
        model = os.environ.get("ENDOSCAN_AGENT_MODEL", "gpt-5.4-mini").strip()
        default_input_rate = "0.75" if model == "gpt-5.4-mini" else "0"
        default_output_rate = "4.50" if model == "gpt-5.4-mini" else "0"
        return cls(
            provider=provider,
            model=model,
            run_mode=AgentRunMode(mode),
            api_key=SecretStr(key) if key else None,
            maximum_turns=int(os.environ.get("ENDOSCAN_AGENT_MAX_TURNS", "6")),
            maximum_tool_calls=int(os.environ.get("ENDOSCAN_AGENT_MAX_TOOL_CALLS", "6")),
            timeout_seconds=float(os.environ.get("ENDOSCAN_AGENT_TIMEOUT_SECONDS", "120")),
            maximum_input_tokens=int(os.environ.get("ENDOSCAN_AGENT_MAX_INPUT_TOKENS", "8000")),
            maximum_output_tokens=int(os.environ.get("ENDOSCAN_AGENT_MAX_OUTPUT_TOKENS", "1500")),
            maximum_cost_usd=float(os.environ.get("ENDOSCAN_AGENT_MAX_COST_USD", "0.20")),
            retry_count=int(os.environ.get("ENDOSCAN_AGENT_RETRY_COUNT", "0")),
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
        return self.provider == "openai" and self.api_key_present

    def public_status(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "provider": self.provider,
            "model": self.model,
            "run_mode": self.run_mode.value,
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
        }


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
