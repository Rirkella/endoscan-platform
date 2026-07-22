from __future__ import annotations

import hashlib
import json
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from endoscan_api import create_app
from endoscan_workflows.contracts import (
    EndpointBuildCreate,
    WorkflowKind,
    WorkflowState,
    WorkflowStatus,
)
from endoscan_workflows.discovery_strategy import (
    ArtifactReference,
    DiscoveryBudgets,
    DiscoveryTaskStatus,
    EvidenceRole,
    HydratedSource,
    HydrationCompleteness,
    OperationalCapabilityRegistry,
    SourceCandidate,
    build_discovery_plan,
    initial_execution_ledger,
    load_operational_capability_registry,
)
from endoscan_workflows.models import EndpointBuildRow, TrainingDatasetWorkflowRow
from endoscan_workflows.preflight import ProviderAccessPreflight
from endoscan_workflows.repository import canonical_json, versioned_payload
from endoscan_workflows.semantics_v2_executor import DiscoveryTaskMaterialization
from endoscan_workflows.source_probe import GeoValidationProbe
from endoscan_workflows.source_security import ScientificSourceClient

ADMIN = {"X-EndoScan-Admin": "local-development"}


def configure(monkeypatch, tmp_path, *, enabled=True):
    monkeypatch.setenv("ENDOSCAN_ADMIN_MODE", "development" if enabled else "disabled")
    monkeypatch.setenv("ENDOSCAN_WORKFLOW_DB", str(tmp_path / "workflows.db"))
    monkeypatch.setenv("ENDOSCAN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))


def post(client, path, payload, key):
    return client.post(path, json=payload, headers={**ADMIN, "Idempotency-Key": key})


def create(client, key="api-phase0-create"):
    response = post(
        client,
        "/admin/endpoint-builds",
        {
            "endpoint_name": "Oxidative stress",
            "endpoint_slug": "oxidative-stress",
            "biological_goal": (
                "Evaluate a response-defined oxidative-stress endpoint from transcriptomic "
                "signatures."
            ),
            "created_by": "local-admin",
        },
        key,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_endpoint_lifecycle_admin_operations_are_registered(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/admin/endpoint-builds/{build_id}/calculate-combination-coverage",
        "/admin/endpoint-builds/{build_id}/generate-assembly-strategies",
        "/admin/endpoint-builds/{build_id}/approve-assembly-strategy",
        "/admin/endpoint-builds/{build_id}/run-approved-assembly",
        "/admin/endpoint-builds/{build_id}/approve-dataset",
        "/admin/endpoint-builds/{build_id}/review-dataset-revision",
        "/admin/endpoint-builds/{build_id}/run-benchmark",
        "/admin/endpoint-builds/{build_id}/select-and-validate-model",
        "/admin/endpoint-builds/{build_id}/review-models",
        "/admin/endpoint-builds/{build_id}/publish-endpoint",
    }
    assert expected <= set(paths)


def test_production_reviewed_source_persistence_uses_one_database(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        database = client.app.state.workflow_database
        assert client.app.state.artifact_store.database is database
        assert client.app.state.source_cache.database is database
        assert all(
            adapter.artifacts.database is adapter.cache.database is database
            for adapter in client.app.state.reviewed_source_adapters.approved()
        )
        assert client.app.state.reviewed_source_adapters.readiness()["approved_adapter_count"] == 7


def test_training_dataset_draft_api_is_hint_free_and_strategy_locked(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        response = post(
            client,
            "/admin/endpoint-builds",
            {
                "endpoint_name": "Example functional endpoint",
                "endpoint_slug": "example-functional-endpoint",
                "biological_goal": (
                    "Construct public training data linking compounds, structures, "
                    "transcriptomic responses and endpoint activity."
                ),
                "created_by": "local-admin",
                "workflow_kind": "training_dataset_discovery",
                "benchmark_mode": "blind_training_dataset_discovery",
            },
            "api-training-draft",
        )
        assert response.status_code == 200, response.text
        build = response.json()
        assert build["current_stage"] == "DRAFT"
        assert build["workflow_kind"] == "training_dataset_discovery"
        workflow = client.get(
            f"/admin/endpoint-builds/{build['id']}/training-dataset-workflow",
            headers=ADMIN,
        )
        assert workflow.status_code == 200, workflow.text
        body = workflow.json()
        assert body["initial_context"]["source_hints"] == []
        assert body["initial_context"]["article_hint"] is None
        assert body["initial_context"]["assay_id_hint"] is None
        assert body["initial_context"]["expected_overlap_hint"] is None
        assert body["initial_context"]["allowed_tools"]
        hints = body["initial_context"]["endpoint_request_semantic_hints"]
        assert hints["requested_endpoint_name"] == "Example functional endpoint"
        assert hints["core_definition_status"] == "insufficient"
        assert body["verified_source_inventory"] is None
        assert body["assembly_strategies"] is None
        artifacts = client.get(
            f"/admin/endpoint-builds/{build['id']}/artifacts", headers=ADMIN
        ).json()
        assert "endpoint_request_semantic_hints" in {item["artifact_type"] for item in artifacts}
        runs = client.get(f"/admin/endpoint-builds/{build['id']}/agent-runs", headers=ADMIN)
        assert runs.json() == []


def test_source_discovery_authorization_api_is_utf8_versioned_and_fail_closed(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_WORKER_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder-never-read")
    app = create_app(repo_root)
    with TestClient(app) as client:
        created = post(
            client,
            "/admin/endpoint-builds",
            {
                "endpoint_name": "Récepteur exemple antagonist",
                "endpoint_slug": "utf8-reviewed-source-authorization",
                "biological_goal": (
                    "Construire un jeu public reliant composés, structures, réponses "
                    "transcriptomiques et activité de l’endpoint."
                ),
                "created_by": "administrateur-é",
                "workflow_kind": "training_dataset_discovery",
                "benchmark_mode": "blind_training_dataset_discovery",
            },
            "utf8-reviewed-source-create",
        ).json()
        rejected = post(
            client,
            f"/admin/endpoint-builds/{created['id']}/authorize-source-discovery",
            {
                "expected_version": created["version"],
                "actor": "administrateur-é",
                "confirmation": "authorize_reviewed_source_discovery",
            },
            "utf8-reviewed-source-rejected",
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"] == "invalid_transition"

        stale = post(
            client,
            f"/admin/endpoint-builds/{created['id']}/authorize-source-discovery",
            {
                "expected_version": created["version"] + 1,
                "actor": "administrateur-é",
                "confirmation": "authorize_reviewed_source_discovery",
            },
            "utf8-reviewed-source-stale",
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "stale_workflow_version"
        assert (
            client.get(f"/admin/endpoint-builds/{created['id']}/agent-runs", headers=ADMIN).json()
            == []
        )


def test_training_dataset_start_compiles_specification_and_persists_approval(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        created = post(
            client,
            "/admin/endpoint-builds",
            {
                "endpoint_name": "X receptor antagonist",
                "endpoint_slug": "example-functional-endpoint-start",
                "biological_goal": (
                    "Construct a compound-level source-neutral public training plan with "
                    "transcriptomic responses."
                ),
                "created_by": "local-admin",
                "workflow_kind": "training_dataset_discovery",
                "benchmark_mode": "blind_training_dataset_discovery",
            },
            "api-training-start-create",
        ).json()
        started = post(
            client,
            f"/admin/endpoint-builds/{created['id']}/start",
            {"expected_version": 0, "actor": "local-admin"},
            "api-training-start",
        )
        assert started.status_code == 200, started.text
        assert started.json()["current_stage"] == "AWAITING_DATASET_SPECIFICATION_REVIEW"
        runs = client.get(
            f"/admin/endpoint-builds/{created['id']}/agent-runs", headers=ADMIN
        ).json()
        assert runs == []
        approvals = client.get(
            f"/admin/endpoint-builds/{created['id']}/approvals", headers=ADMIN
        ).json()
        assert approvals[-1]["approval_type"] == "dataset_specification"
        assert approvals[-1]["status"] == "pending"


def test_broad_endpoint_retry_accepts_human_discovery_scope_without_provider_calls(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        created = post(
            client,
            "/admin/endpoint-builds",
            {
                "endpoint_name": "Thyroid hormone receptor activity",
                "endpoint_slug": "thyroid-receptor-activity-broad-api",
                "biological_goal": (
                    "Determine which public compound-level thyroid hormone receptor activity "
                    "modalities can connect to compound-induced transcriptomic responses."
                ),
                "created_by": "local-admin",
                "workflow_kind": "training_dataset_discovery",
                "benchmark_mode": "blind_training_dataset_discovery",
            },
            "api-broad-create",
        ).json()
        revision = post(
            client,
            f"/admin/endpoint-builds/{created['id']}/start",
            {"expected_version": created["version"], "actor": "local-admin"},
            "api-broad-start",
        ).json()
        assert revision["current_stage"] == "AWAITING_DATASET_SPECIFICATION_REVISION"
        waiting = post(
            client,
            f"/admin/endpoint-builds/{created['id']}/retry-dataset-specification",
            {
                "expected_version": revision["version"],
                "actor": "local-admin",
                "endpoint_discovery_scope": {
                    "schema_version": "1.0.0",
                    "mode": "broad_modality_exploration",
                    "biological_target": "Thyroid hormone receptor",
                    "fixed_modality": None,
                    "candidate_modalities": ["binding", "agonism", "antagonism"],
                    "explicitly_excluded_modalities": [],
                    "preserve_modalities_separately": True,
                    "aggregation_allowed_later": True,
                    "aggregation_requires_human_approval": True,
                    "aggregation_active_during_discovery": False,
                    "selection_deferred_until": "assembly_strategy_review",
                    "scientific_scope": (
                        "Explore binding, agonism, and antagonism separately and defer endpoint "
                        "selection."
                    ),
                    "provenance": ["human_scoped_configuration"],
                },
            },
            "api-broad-retry",
        )
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["current_stage"] == "AWAITING_DATASET_SPECIFICATION_REVIEW"
        workflow = client.get(
            f"/admin/endpoint-builds/{created['id']}/training-dataset-workflow",
            headers=ADMIN,
        ).json()
        assert workflow["endpoint_discovery_scope"]["candidate_modalities"] == [
            "binding",
            "agonism",
            "antagonism",
        ]
        assert workflow["specification_draft"]["endpoint_modality"] is None
        assert workflow["component_requirements"] is None
        approvals = client.get(
            f"/admin/endpoint-builds/{created['id']}/approvals", headers=ADMIN
        ).json()
        assert approvals[-1]["approval_type"] == "dataset_specification"
        assert approvals[-1]["status"] == "pending"
        assert (
            client.get(f"/admin/endpoint-builds/{created['id']}/agent-runs", headers=ADMIN).json()
            == []
        )


def test_specialized_boundary_probes_are_zero_network(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        response = post(
            client,
            "/admin/agent-provider/specialized-boundary-probes",
            {},
            "specialized-boundary-probes",
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["network_requests"] == 0
        assert len(body["results"]) == 7
        assert all(item["network_requests"] == 0 for item in body["results"])
        assert all(item["model_call_boundary_reached"] for item in body["results"])
        specification = next(
            item
            for item in body["results"]
            if item["agent_name"] == "Dataset Specification Review Agent"
        )
        assert specification["semantic_hints_present"] is True


def test_admin_routes_fail_closed_outside_development(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path, enabled=False)
    with TestClient(create_app(repo_root)) as client:
        response = client.get("/admin/endpoint-builds", headers=ADMIN)
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"]


def test_admin_requires_explicit_local_marker(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        assert client.get("/admin/endpoint-builds").status_code == 401
        assert client.get("/admin/endpoint-builds", headers=ADMIN).status_code == 200


def test_provider_preflight_requires_development_mode(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path, enabled=False)
    app = create_app(repo_root)

    class ForbiddenPreflight:
        def check(self):
            raise AssertionError("Preflight must not execute outside development mode")

    app.state.provider_preflight = ForbiddenPreflight()
    with TestClient(app) as client:
        response = post(client, "/admin/agent-provider/preflight", {}, "preflight-disabled")
        assert response.status_code == 403


def test_provider_preflight_is_explicit_and_creates_no_workflow_state(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    app = create_app(repo_root)

    class FakeClient:
        retrieve_calls: list[str] = []

        def __init__(self, **_kwargs):
            self.models = SimpleNamespace(with_raw_response=SimpleNamespace(retrieve=self.retrieve))

        @property
        def responses(self):
            raise AssertionError("Preflight must never use Responses generation")

        def retrieve(self, model: str):
            self.retrieve_calls.append(model)
            return SimpleNamespace(
                parse=lambda: SimpleNamespace(id=model),
                http_response=SimpleNamespace(
                    status_code=200,
                    headers={"x-request-id": "req_preflight-safe"},
                ),
            )

        def close(self):
            return None

    class ForbiddenGeoClient:
        def __getattr__(self, _name):
            raise AssertionError("Preflight must never access GEO")

    fake_client = FakeClient()
    app.state.provider_preflight = ProviderAccessPreflight(
        app.state.agent_configuration,
        client_factory=lambda **_kwargs: fake_client,
    )
    app.state.source_client = ForbiddenGeoClient()
    with TestClient(app) as client:
        assert fake_client.retrieve_calls == []
        with app.state.workflow_database.session() as session:
            before = {
                "builds": session.execute(
                    text("select count(*) from endpoint_builds")
                ).scalar_one(),
                "runs": session.execute(text("select count(*) from agent_runs")).scalar_one(),
            }
        response = post(client, "/admin/agent-provider/preflight", {}, "preflight-success")
        assert response.status_code == 200
        assert response.json()["generation_capability"] == "not_checked"
        assert response.json()["billing_status"] == "not_checked"
        assert fake_client.retrieve_calls == ["gpt-5.4-mini"]
        with app.state.workflow_database.session() as session:
            after = {
                "builds": session.execute(
                    text("select count(*) from endpoint_builds")
                ).scalar_one(),
                "runs": session.execute(text("select count(*) from agent_runs")).scalar_one(),
            }
        assert after == before == {"builds": 0, "runs": 0}
        assert "unit-test" not in response.text.lower()
        assert "authorization" not in response.text.lower()


def test_provider_preflight_missing_key_makes_no_external_call(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "live")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(repo_root)) as client:
        response = post(client, "/admin/agent-provider/preflight", {}, "preflight-no-key")
        assert response.status_code == 200
        payload = response.json()
        assert payload["api_key_present"] is False
        assert payload["authentication_accepted"] is False
        assert payload["model_accessible"] is False
        assert payload["provider_error_code"] == "missing_api_key"
        assert payload["generation_capability"] == "not_checked"


def test_adapter_boundary_probe_reaches_model_boundary_without_state_or_network(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)

    def forbidden_external_call(*args, **kwargs):
        raise AssertionError("Boundary probe attempted an external request")

    monkeypatch.setattr("endoscan_workflows.openai_provider.AsyncOpenAI", forbidden_external_call)
    monkeypatch.setattr(
        "endoscan_workflows.source_security.ScientificSourceClient.get",
        forbidden_external_call,
    )
    with TestClient(create_app(repo_root)) as client:
        response = post(
            client,
            "/admin/agent-provider/boundary-probe",
            {},
            "adapter-boundary-probe",
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload == {
            "schema_version": "1.0.0",
            "local_sdk_configuration_valid": True,
            "model_call_boundary_reached": True,
            "developer_message": None,
            "exception_class": None,
            "sdk_version": "0.18.2",
            "model": "gpt-5.4-mini",
            "tool_count": 1,
            "output_schema_name": "DiscoveryOutput",
            "network_requests": 0,
        }
        assert client.get("/admin/endpoint-builds", headers=ADMIN).json() == []


def test_geo_validation_probe_uses_production_validator_without_agent_state(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    captured: list[tuple[str, dict[str, str]]] = []
    soft = "\n".join(
        [
            "^SERIES = GSE314406",
            "!Series_geo_accession = GSE314406",
            "!Series_status = Public on Jul 18 2026",
            "!Series_title = Official probe fixture",
            "!Series_type = Expression profiling by high throughput sequencing",
            "!Series_sample_organism_ch1 = Homo sapiens",
        ]
    )

    def official_geo(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, dict(request.url.params)))
        return httpx.Response(
            200,
            text=soft,
            headers={"content-type": "text/plain"},
            request=request,
        )

    source_client = ScientificSourceClient(
        transport=httpx.MockTransport(official_geo), sleep=lambda _seconds: None
    )
    app = create_app(repo_root)
    app.state.geo_validation_probe = GeoValidationProbe(
        artifact_root=tmp_path.parents[1] / f"probe-{tmp_path.name[-8:]}",
        source_client=source_client,
        ttl_seconds=86_400,
    )
    try:
        with TestClient(app) as client:
            with app.state.workflow_database.session() as session:
                before = {
                    "builds": session.execute(
                        text("select count(*) from endpoint_builds")
                    ).scalar_one(),
                    "runs": session.execute(text("select count(*) from agent_runs")).scalar_one(),
                }
            response = post(
                client,
                "/admin/source-tools/geo-validation-probe",
                {"accessions": ["GSE314406"]},
                "geo-validation-probe",
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["openai_calls"] == 0
            assert payload["workflow_builds_created"] == 0
            assert payload["agent_runs_created"] == 0
            assert payload["results"][0]["status"] == "public_valid"
            assert payload["results"][0]["title"] == "Official probe fixture"
            with app.state.workflow_database.session() as session:
                after = {
                    "builds": session.execute(
                        text("select count(*) from endpoint_builds")
                    ).scalar_one(),
                    "runs": session.execute(text("select count(*) from agent_runs")).scalar_one(),
                }
            assert after == before == {"builds": 0, "runs": 0}
            assert captured == [
                (
                    "/geo/query/acc.cgi",
                    {
                        "acc": "GSE314406",
                        "targ": "self",
                        "view": "brief",
                        "form": "text",
                    },
                )
            ]
    finally:
        source_client.close()


def test_geo_validation_probe_rejects_arbitrary_url_before_execution(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    app = create_app(repo_root)

    class ForbiddenProbe:
        def run(self, _accessions):
            raise AssertionError("Invalid probe input must not reach the source validator")

    app.state.geo_validation_probe = ForbiddenProbe()
    with TestClient(app) as client:
        response = post(
            client,
            "/admin/source-tools/geo-validation-probe",
            {"accessions": ["https://example.com/GSE314406"]},
            "invalid-geo-probe",
        )
    assert response.status_code == 422


def test_full_phase0_api_workflow(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path)
    registry = repo_root / "registry" / "models" / "endpoints.json"
    registry_before = hashlib.sha256(registry.read_bytes()).hexdigest()
    with TestClient(create_app(repo_root)) as client:
        health = client.get("/health").json()
        assert health["workflow_database"]["journal_mode"] == "wal"
        assert health["workflow_database"]["foreign_keys"] is True
        assert health["agent_provider"]["configured"] == ["fake", "openai"]
        capabilities = client.get("/admin/capabilities", headers=ADMIN).json()
        assert capabilities["provider"] == "fake"
        assert capabilities["run_mode"] == "replay"
        assert capabilities["api_key_present"] is False
        assert capabilities["source_tools_available"] is True
        assert capabilities["configured_budget"]["retry_count"] == 0
        assert "api_key" not in capabilities
        assert health["agent_provider"]["live_model_api"] is False
        assert health["admin"]["development_mode"] is True

        build = create(client)
        assert build["current_stage"] == "DRAFT"
        assert client.get("/admin/endpoint-builds", headers=ADMIN).json()[0]["id"] == build["id"]
        assert (
            client.get(f"/admin/endpoint-builds/{build['id']}", headers=ADMIN).json()["id"]
            == build["id"]
        )

        started_response = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/start",
            {"expected_version": build["version"], "actor": "local-admin"},
            "api-phase0-start",
        )
        assert started_response.status_code == 200, started_response.text
        waiting = started_response.json()
        assert waiting["current_stage"] == "AWAITING_DATASET_APPROVAL"
        assert waiting["pending_approval_id"]

        steps = client.get(f"/admin/endpoint-builds/{build['id']}/steps", headers=ADMIN).json()
        assert steps[0]["status"] == "completed"
        artifacts = client.get(
            f"/admin/endpoint-builds/{build['id']}/artifacts", headers=ADMIN
        ).json()
        candidate = next(
            item for item in artifacts if item["artifact_type"] == "dataset_candidates"
        )
        preview = client.get(f"/admin/artifacts/{candidate['id']}/preview", headers=ADMIN).json()
        assert preview["content"]["live_discovery"] is False
        assert len(preview["content"]["candidates"]) >= 2

        runs = client.get(f"/admin/endpoint-builds/{build['id']}/agent-runs", headers=ADMIN).json()
        trace = client.get(f"/admin/agent-runs/{runs[0]['id']}", headers=ADMIN).json()
        assert trace["provider"] == "fake"
        assert trace["run_mode"] == "replay"
        assert len(trace["tools"]) == 4
        assert all(item["tool_name"] != "shell" for item in trace["tools"])

        approval = client.get(
            f"/admin/approvals/{waiting['pending_approval_id']}", headers=ADMIN
        ).json()
        approved_response = post(
            client,
            f"/admin/approvals/{approval['id']}/decisions",
            {
                "decision": "approve",
                "reviewer_id": "local-admin",
                "reviewer_comment": "Reviewed prepared fixture limitations.",
                "expected_version": waiting["version"],
                "artifact_hashes": approval["request"]["artifact_hashes"],
            },
            "api-phase0-approve",
        )
        assert approved_response.status_code == 200, approved_response.text
        curating = approved_response.json()
        assert curating["current_stage"] == "CURATING_DATA"

        paused = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/pause",
            {"expected_version": curating["version"], "actor": "local-admin"},
            "api-phase0-pause",
        ).json()
        assert paused["paused_from_state"] == "CURATING_DATA"
        resumed = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/resume",
            {"expected_version": paused["version"], "actor": "local-admin"},
            "api-phase0-resume",
        ).json()
        assert resumed["current_stage"] == "CURATING_DATA"

        failed = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/simulate-failure",
            {"expected_version": resumed["version"], "actor": "local-admin"},
            "api-phase0-failure",
        ).json()
        assert failed["current_stage"] == "FAILED"
        retried = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/retry",
            {"expected_version": failed["version"], "actor": "local-admin"},
            "api-phase0-retry",
        ).json()
        assert retried["current_stage"] == "CURATING_DATA"
        assert (
            len(
                client.get(f"/admin/endpoint-builds/{build['id']}/agent-runs", headers=ADMIN).json()
            )
            == 1
        )

        timeline = client.get(
            f"/admin/endpoint-builds/{build['id']}/timeline", headers=ADMIN
        ).json()
        assert any(item["event_type"] == "approval.decided" for item in timeline)
        assert any(item["event_type"] == "workflow.retried" for item in timeline)
        errors = client.get(f"/admin/endpoint-builds/{build['id']}/errors", headers=ADMIN).json()
        assert errors[-1]["code"] == "phase0_controlled_failure"

        assert client.get("/admin/endpoint-builds/../../etc/passwd", headers=ADMIN).status_code in {
            404,
            405,
        }
        assert client.get("/admin/artifacts/not-an-id", headers=ADMIN).status_code == 404
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == registry_before


def test_semantics_v2_review_actions_are_thin_typed_and_idempotency_bound(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        build = create(client, key="api-semantics-v2-actions")
        service = client.app.state.workflow_service
        snapshot = service.get_build(build["id"])
        service.request_semantics_v2_discovery_revision = MagicMock(return_value=snapshot)
        service.approve_semantics_v2_strategy = MagicMock(return_value=snapshot)
        service.reject_semantics_v2_strategy_set = MagicMock(return_value=snapshot)

        revision = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/request-discovery-revision",
            {
                "expected_version": build["version"],
                "actor": "local-admin",
                "reason": "Expand reviewed provider coverage.",
                "provider_policy_revision": {"include_providers": ["provider-a"]},
                "modality_policy_revision": {},
                "context_constraint_revision": {},
            },
            "api-semantics-v2-revision",
        )
        assert revision.status_code == 200, revision.text
        service.request_semantics_v2_discovery_revision.assert_called_once()

        approval = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/approve-assembly-strategy",
            {
                "expected_version": build["version"],
                "actor": "local-admin",
                "strategy_proposal_id": "proposal-1",
                "approved_modality_aggregation": {"mode": "none"},
                "approved_context_filters": {"cell": "reviewed"},
                "approved_dose_time_rules": {"preserve_missingness": True},
                "approved_label_policy": {"rule": "source-backed"},
                "exclusion_rules": ["ambiguous"],
                "required_extraction_fields": ["canonical_compound_identifier"],
            },
            "api-semantics-v2-approve",
        )
        assert approval.status_code == 200, approval.text
        service.approve_semantics_v2_strategy.assert_called_once()

        rejection = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/reject-assembly-strategies",
            {
                "expected_version": build["version"],
                "actor": "local-admin",
                "reason": "No current proposal is scientifically acceptable.",
            },
            "api-semantics-v2-reject",
        )
        assert rejection.status_code == 200, rejection.text
        service.reject_semantics_v2_strategy_set.assert_called_once()

        invalid = post(
            client,
            f"/admin/endpoint-builds/{build['id']}/request-discovery-revision",
            {"expected_version": build["version"], "actor": "local-admin", "reason": "x"},
            "api-semantics-v2-invalid",
        )
        assert invalid.status_code == 422


def test_restart_preserves_approval_artifacts_and_trace(repo_root, monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as first:
        build = create(first, key="api-restart-create")
        waiting = post(
            first,
            f"/admin/endpoint-builds/{build['id']}/start",
            {"expected_version": 0, "actor": "local-admin"},
            "api-restart-start",
        ).json()
        artifact_ids = {
            item["id"]
            for item in first.get(
                f"/admin/endpoint-builds/{build['id']}/artifacts", headers=ADMIN
            ).json()
        }
        event_count = len(
            first.get(f"/admin/endpoint-builds/{build['id']}/timeline", headers=ADMIN).json()
        )
    with TestClient(create_app(repo_root)) as restarted:
        restored = restarted.get(f"/admin/endpoint-builds/{build['id']}", headers=ADMIN).json()
        assert restored["current_stage"] == "AWAITING_DATASET_APPROVAL"
        assert restored["pending_approval_id"] == waiting["pending_approval_id"]
        assert {
            item["id"]
            for item in restarted.get(
                f"/admin/endpoint-builds/{build['id']}/artifacts", headers=ADMIN
            ).json()
        } == artifact_ids
        assert (
            len(
                restarted.get(
                    f"/admin/endpoint-builds/{build['id']}/timeline", headers=ADMIN
                ).json()
            )
            == event_count
        )


def test_existing_continuation_api_executes_semantics_v2_discovery_and_hydration(
    repo_root, monkeypatch, tmp_path
) -> None:
    configure(monkeypatch, tmp_path)
    with TestClient(create_app(repo_root)) as client:
        service = client.app.state.workflow_service
        executor = client.app.state.semantics_v2_discovery_executor
        created = service.create_build(
            EndpointBuildCreate(
                endpoint_name="API semantics v2 bridge",
                endpoint_slug="api-semantics-v2-bridge",
                biological_goal="Exercise the existing API continuation through both new states.",
                created_by="api-test",
                workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
                benchmark_mode="blind_training_dataset_discovery",
                idempotency_key="api-semantics-v2-bridge-create",
            )
        )
        descriptor = client.app.state.artifact_store.put_json(
            workflow_id=created.id,
            value={"approved": True},
            artifact_type="training_dataset_specification",
            logical_name="api-semantics-v2-approved-specification.json",
            producer="api-test",
            idempotency_key="api-semantics-v2-approved-specification",
        )
        specification = ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )
        production_registry = load_operational_capability_registry(repo_root)
        registry = OperationalCapabilityRegistry(
            providers=[production_registry.by_provider("tox21")]
        )
        plan = build_discovery_plan(
            workflow_id=created.id,
            endpoint_identifier="api-semantics-v2-bridge",
            approved_specification_artifact=specification,
            discovery_round=0,
            biological_target="Example receptor",
            target_synonyms=[],
            requested_modalities=["binding"],
            registry=registry,
            budgets=DiscoveryBudgets(
                maximum_provider_invocations=1,
                maximum_tool_calls=1,
                maximum_scientific_source_requests=1,
                maximum_input_tokens=0,
                maximum_output_tokens=0,
                maximum_estimated_cost_usd=0,
                timeout_seconds=30,
            ),
        )
        ledger, _findings = initial_execution_ledger(plan, registry)
        with client.app.state.workflow_database.session() as session:
            build = session.get(EndpointBuildRow, created.id)
            workflow = session.get(TrainingDatasetWorkflowRow, created.id)
            assert build is not None and workflow is not None
            build.current_stage = WorkflowState.DISCOVERING_SOURCE_CANDIDATES.value
            build.status = WorkflowStatus.ACTIVE.value
            build.version = 1
            workflow.workflow_semantics_version = "2.0.0"
            workflow.discovery_round = 0
            workflow.discovery_plan_json = canonical_json(
                versioned_payload(document=plan.model_dump(mode="json"))
            )
            workflow.discovery_execution_ledger_json = canonical_json(
                versioned_payload(document=ledger.model_dump(mode="json"))
            )

        def materialize_fixture(self, workflow_id, active_plan, task, running):
            candidate_id = f"candidate-{task.task_id}"
            terminal = running.model_copy(
                update={
                    "status": DiscoveryTaskStatus.COMPLETED,
                    "search_result_count": 1,
                    "unique_candidate_count": 1,
                    "completion_reason": "Offline typed-provider fixture completed.",
                    "source_response_artifacts": [specification],
                    "logical_tool_call_count": 1,
                }
            )
            candidate = SourceCandidate(
                candidate_id=candidate_id,
                provider=task.provider,
                source_identifier="AID:fixture",
                evidence_role=EvidenceRole.ACTIVITY,
                modality=task.modality,
                discovery_task_id=task.task_id,
                contributing_task_ids=[task.task_id],
                contributing_modalities=([task.modality] if task.modality else []),
                release_id="offline-fixture-v1",
                source_version="offline-v1",
                discovery_query_provenance=[specification],
                provider_dataset_artifacts=[specification],
                coverage_summary={"by_task": {task.task_id: {"assay_count": 1}}},
                candidate_status="metadata_candidate",
            )
            hydrated = HydratedSource(
                hydrated_source_id=f"hydrated-{task.task_id}",
                source_candidate_id=candidate_id,
                provider=task.provider,
                source_identifier="AID:fixture",
                evidence_role=EvidenceRole.ACTIVITY,
                modality=task.modality,
                contributing_task_ids=[task.task_id],
                contributing_modalities=([task.modality] if task.modality else []),
                release_id="offline-fixture-v1",
                verified_metadata={
                    "offline_fixture": True,
                    "scientific_source_unit": "assay_endpoint",
                },
                compound_index_available=True,
                label_or_activity_fields_available=True,
                source_version="offline-v1",
                licence_and_provenance=["offline fixture"],
                completeness_status=HydrationCompleteness.COMPLETE,
                coverage_summary={"by_task": {task.task_id: {"assay_count": 1}}},
                evidence_artifacts=[specification],
            )
            return DiscoveryTaskMaterialization(
                workflow_id=workflow_id,
                discovery_round=active_plan.discovery_round,
                plan_fingerprint=active_plan.plan_fingerprint,
                task_id=task.task_id,
                ledger_record=terminal,
                candidates=[candidate],
                hydrated_sources=[hydrated],
            )

        monkeypatch.setattr(
            executor,
            "_invoke_task",
            MethodType(materialize_fixture, executor),
        )
        discovered = post(
            client,
            f"/admin/endpoint-builds/{created.id}/continue-training-dataset",
            {"expected_version": 1, "actor": "api-test"},
            "api-semantics-v2-discovery",
        )
        assert discovered.status_code == 200, discovered.text
        discovered_body = discovered.json()
        assert discovered_body["current_stage"] == "HYDRATING_SOURCE_CANDIDATES"
        assert discovered_body["discovery_progress"]["candidate_count"] == 1
        assert discovered_body["discovery_progress"]["hydrated_source_count"] == 0
        assert discovered_body["discovery_progress"]["artifact_references"]
        assert discovered_body["discovery_progress"]["completed_task_count"] == 1
        assert discovered_body["discovery_progress"]["partial_task_count"] == 0
        assert discovered_body["discovery_progress"]["failed_task_count"] == 0
        assert discovered_body["discovery_progress"]["running_task_count"] == 0
        assert discovered_body["discovery_progress"]["blocked_task_count"] == 0
        assert discovered_body["discovery_progress"]["pending_task_count"] == 0
        assert set(discovered_body["discovery_progress"]["ledger_task_totals_by_status"]) == {
            item.value for item in DiscoveryTaskStatus
        }
        assert "activity_records" not in json.dumps(
            discovered_body["discovery_progress"], sort_keys=True
        )

        hydrated = post(
            client,
            f"/admin/endpoint-builds/{created.id}/continue-training-dataset",
            {"expected_version": discovered_body["version"], "actor": "api-test"},
            "api-semantics-v2-hydration",
        )
        assert hydrated.status_code == 200, hydrated.text
        hydrated_body = hydrated.json()
        assert hydrated_body["current_stage"] == "COMPUTING_COMBINATION_COVERAGE"
        assert hydrated_body["discovery_progress"]["candidate_count"] == 1
        assert hydrated_body["discovery_progress"]["hydrated_source_count"] == 1
        workflow = service.training_dataset_workflow(created.id)
        assert workflow["combination_coverage"] is None
        assert workflow["strategy_proposals"] is None
        assert workflow["assembly_recipe"] is None
