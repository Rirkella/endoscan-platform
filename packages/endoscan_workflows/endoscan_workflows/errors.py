"""Safe domain errors mapped by the thin admin API."""


class WorkflowError(RuntimeError):
    code = "workflow_error"
    status_code = 400

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.safe_message = message
        self.detail = detail or {}


class WorkflowNotFound(WorkflowError):
    code = "not_found"
    status_code = 404


class WorkflowConflict(WorkflowError):
    code = "conflict"
    status_code = 409


class InvalidTransition(WorkflowConflict):
    code = "invalid_transition"


class StaleWorkflowVersion(WorkflowConflict):
    code = "stale_workflow_version"


class GuardNotSatisfied(WorkflowConflict):
    code = "guard_not_satisfied"


class ArtifactError(WorkflowError):
    code = "artifact_error"
    status_code = 422


class ArtifactTooLarge(ArtifactError):
    code = "artifact_too_large"
    status_code = 413


class ArtifactIntegrityError(ArtifactError):
    code = "artifact_integrity_error"
    status_code = 409


class AgentPolicyError(WorkflowError):
    code = "agent_policy_error"
    status_code = 422
