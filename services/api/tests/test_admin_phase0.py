from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from endoscan_api import create_app
from endoscan_workflows.preflight import ProviderAccessPreflight
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
