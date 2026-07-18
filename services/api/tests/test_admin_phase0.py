from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from endoscan_api import create_app

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
