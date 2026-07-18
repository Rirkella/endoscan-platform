"""Thin protected admin routes over the durable workflow service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from endoscan_workflows.contracts import (
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    WorkflowState,
)

from .auth import (
    require_development_admin,
    require_mutation_budget,
    validate_resource_id,
)
from .schemas import (
    ApprovalDecisionBody,
    CreateBuildBody,
    GeoValidationProbeBody,
    WorkflowCommandBody,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin-development"],
    dependencies=[Depends(require_development_admin)],
)


def _service(request: Request):
    return request.app.state.workflow_service


def _key(idempotency_key: str = Header(alias="Idempotency-Key", min_length=8)) -> str:
    return idempotency_key


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    return request.app.state.agent_capabilities


@router.post("/agent-provider/preflight", dependencies=[Depends(require_mutation_budget)])
def provider_preflight(request: Request):
    return request.app.state.provider_preflight.check()


@router.post("/agent-provider/boundary-probe", dependencies=[Depends(require_mutation_budget)])
def adapter_boundary_probe(request: Request):
    return request.app.state.adapter_boundary_probe.check()


@router.post("/source-tools/geo-validation-probe", dependencies=[Depends(require_mutation_budget)])
def geo_validation_probe(body: GeoValidationProbeBody, request: Request):
    return request.app.state.geo_validation_probe.run(body.accessions)


@router.post("/endpoint-builds", dependencies=[Depends(require_mutation_budget)])
def create_build(
    body: CreateBuildBody,
    request: Request,
    idempotency_key: str = Depends(_key),
):
    return _service(request).create_build(
        EndpointBuildCreate(**body.model_dump(), idempotency_key=idempotency_key)
    )


@router.get("/endpoint-builds")
def list_builds(request: Request):
    return _service(request).list_builds()


@router.get("/endpoint-builds/{build_id}")
def get_build(build_id: str, request: Request):
    return _service(request).get_build(validate_resource_id(build_id, "Endpoint build"))


def _command(action: str, build_id: str, body: WorkflowCommandBody, request: Request, key: str):
    service = _service(request)
    build_id = validate_resource_id(build_id, "Endpoint build")
    kwargs = {
        "expected_version": body.expected_version,
        "actor": body.actor,
        "idempotency_key": key,
    }
    return getattr(service, action)(build_id, **kwargs)


@router.post("/endpoint-builds/{build_id}/start", dependencies=[Depends(require_mutation_budget)])
def start(build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)):
    return _command("start_build", build_id, body, request, key)


@router.post("/endpoint-builds/{build_id}/pause", dependencies=[Depends(require_mutation_budget)])
def pause(build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)):
    return _command("pause", build_id, body, request, key)


@router.post("/endpoint-builds/{build_id}/resume", dependencies=[Depends(require_mutation_budget)])
def resume(build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)):
    return _command("resume", build_id, body, request, key)


@router.post("/endpoint-builds/{build_id}/cancel", dependencies=[Depends(require_mutation_budget)])
def cancel(build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)):
    return _command("cancel", build_id, body, request, key)


@router.post("/endpoint-builds/{build_id}/retry", dependencies=[Depends(require_mutation_budget)])
def retry(build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)):
    return _command("retry_failed", build_id, body, request, key)


@router.post(
    "/endpoint-builds/{build_id}/refresh-source-metadata",
    dependencies=[Depends(require_mutation_budget)],
)
def refresh_source_metadata(
    build_id: str,
    body: WorkflowCommandBody,
    request: Request,
    key: str = Depends(_key),
):
    service = _service(request)
    build_id = validate_resource_id(build_id, "Endpoint build")
    build = service.get_build(build_id)
    if build.current_stage is not WorkflowState.AWAITING_DATASET_APPROVAL:
        return service.run_discovery(
            build_id,
            expected_version=body.expected_version,
            actor=body.actor,
            idempotency_key=f"{key}:refresh",
            refresh_source_metadata=True,
        )
    pending = service.list_approvals(build_id, pending_only=True)
    if not pending:
        return build
    request_payload = pending[-1]["request"]
    revised = service.decide_approval(
        pending[-1]["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.REQUEST_REVISION,
            reviewer_id=body.actor,
            reviewer_comment="Administrator requested an explicit scientific source refresh.",
            expected_version=body.expected_version,
            idempotency_key=f"{key}:revision",
            artifact_hashes=request_payload["artifact_hashes"],
        ),
    )
    return service.run_discovery(
        build_id,
        expected_version=revised.version,
        actor=body.actor,
        idempotency_key=f"{key}:refresh",
        refresh_source_metadata=True,
    )


@router.post(
    "/endpoint-builds/{build_id}/simulate-failure",
    dependencies=[Depends(require_mutation_budget)],
)
def simulate_failure(
    build_id: str, body: WorkflowCommandBody, request: Request, key: str = Depends(_key)
):
    return _command("simulate_failure", build_id, body, request, key)


@router.get("/endpoint-builds/{build_id}/steps")
def steps(build_id: str, request: Request):
    return _service(request).steps(validate_resource_id(build_id, "Endpoint build"))


@router.get("/endpoint-builds/{build_id}/timeline")
def timeline(build_id: str, request: Request):
    return _service(request).timeline(validate_resource_id(build_id, "Endpoint build"))


@router.get("/endpoint-builds/{build_id}/artifacts")
def artifacts(build_id: str, request: Request):
    return request.app.state.artifact_store.list_artifacts(
        validate_resource_id(build_id, "Endpoint build")
    )


@router.get("/artifacts/{artifact_id}")
def artifact(artifact_id: str, request: Request):
    descriptor, _content = request.app.state.artifact_store.get(
        validate_resource_id(artifact_id, "Artifact")
    )
    return descriptor


@router.get("/artifacts/{artifact_id}/preview")
def artifact_preview(artifact_id: str, request: Request):
    descriptor, content = request.app.state.artifact_store.get(
        validate_resource_id(artifact_id, "Artifact")
    )
    if descriptor.mime_type == "application/json":
        import json

        return {
            "schema_version": "1.0.0",
            "artifact": descriptor,
            "content": json.loads(content),
        }
    if descriptor.mime_type.startswith("text/") and descriptor.size_bytes <= 256_000:
        return {
            "schema_version": "1.0.0",
            "artifact": descriptor,
            "content": content.decode("utf-8", errors="replace"),
        }
    return {
        "schema_version": "1.0.0",
        "artifact": descriptor,
        "content": None,
        "reason": "Binary or oversized artifact preview is disabled.",
    }


@router.get("/endpoint-builds/{build_id}/approvals")
def approvals(build_id: str, request: Request, pending_only: bool = False):
    return _service(request).list_approvals(
        validate_resource_id(build_id, "Endpoint build"), pending_only=pending_only
    )


@router.get("/approvals/{approval_id}")
def approval(approval_id: str, request: Request):
    return _service(request).get_approval(validate_resource_id(approval_id, "Approval"))


@router.post("/approvals/{approval_id}/decisions", dependencies=[Depends(require_mutation_budget)])
def decide(
    approval_id: str,
    body: ApprovalDecisionBody,
    request: Request,
    idempotency_key: str = Depends(_key),
):
    service = _service(request)
    snapshot = service.decide_approval(
        validate_resource_id(approval_id, "Approval"),
        ApprovalDecision(**body.model_dump(), idempotency_key=idempotency_key),
    )
    if snapshot.current_stage is WorkflowState.DISCOVERING_DATA:
        return service.run_discovery(
            snapshot.id,
            expected_version=snapshot.version,
            actor=body.reviewer_id,
            idempotency_key=f"{idempotency_key}:revision",
        )
    return snapshot


@router.get("/endpoint-builds/{build_id}/agent-runs")
def agent_runs(build_id: str, request: Request):
    return _service(request).agent_runs(validate_resource_id(build_id, "Endpoint build"))


@router.get("/agent-runs/{run_id}")
def agent_run(run_id: str, request: Request):
    return _service(request).agent_run(validate_resource_id(run_id, "Agent run"))


@router.get("/endpoint-builds/{build_id}/errors")
def errors(build_id: str, request: Request):
    return _service(request).errors(validate_resource_id(build_id, "Endpoint build"))
