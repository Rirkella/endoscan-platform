"""Canonical workflow graph loader and transition guard metadata."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ActorType, WorkflowState
from .errors import InvalidTransition

WORKFLOW_GRAPH_PATH = Path(__file__).with_name("workflow-state-machine.json")


def canonical_workflow_graph_path() -> Path:
    """Return the state-machine contract owned and packaged with the workflow runtime."""
    return WORKFLOW_GRAPH_PATH


class TransitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_state: WorkflowState = Field(alias="from")
    to_state: WorkflowState = Field(alias="to")
    initiator: ActorType
    guard: str
    required_artifacts: list[str] = Field(default_factory=list)


class WorkflowGraph:
    def __init__(self, source: Path):
        self.source = Path(source)
        raw = json.loads(self.source.read_text(encoding="utf-8"))
        if raw.get("schema_version") != "1.0.0":
            raise ValueError("unsupported workflow state-machine schema")
        declared = {WorkflowState(value) for value in raw["states"]}
        if declared != set(WorkflowState):
            raise ValueError("workflow state-machine states differ from typed WorkflowState")
        self.transitions = [TransitionSpec.model_validate(item) for item in raw["transitions"]]
        self._by_edge = {(item.from_state, item.to_state): item for item in self.transitions}

    def transition(
        self,
        current: WorkflowState,
        target: WorkflowState,
        initiator: ActorType,
    ) -> TransitionSpec | None:
        if target is WorkflowState.PAUSED and current not in {
            WorkflowState.PAUSED,
            WorkflowState.REGISTERING,
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
        }:
            if initiator is not ActorType.HUMAN:
                raise InvalidTransition("Only a human may pause a workflow.")
            return None
        if target is WorkflowState.CANCELLED and current not in {
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
        }:
            if initiator is not ActorType.HUMAN:
                raise InvalidTransition("Only a human may cancel a workflow.")
            return None
        if target is WorkflowState.FAILED and current not in {
            WorkflowState.FAILED,
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
        }:
            if initiator not in {ActorType.ORCHESTRATOR, ActorType.SYSTEM}:
                raise InvalidTransition("Only the orchestrator may record a workflow failure.")
            return None
        spec = self._by_edge.get((current, target))
        if spec is None:
            raise InvalidTransition(
                f"Transition {current.value} -> {target.value} is not allowed.",
                detail={"current_state": current.value, "allowed_states": self.allowed(current)},
            )
        if spec.initiator is not initiator:
            raise InvalidTransition(
                f"Transition {current.value} -> {target.value} requires {spec.initiator.value}."
            )
        return spec

    def allowed(self, current: WorkflowState) -> list[str]:
        allowed = [item.to_state.value for item in self.transitions if item.from_state is current]
        if current not in {
            WorkflowState.PAUSED,
            WorkflowState.REGISTERING,
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
        }:
            allowed.append(WorkflowState.PAUSED.value)
        if current not in {WorkflowState.COMPLETED, WorkflowState.CANCELLED}:
            allowed.append(WorkflowState.CANCELLED.value)
        return sorted(set(allowed))
