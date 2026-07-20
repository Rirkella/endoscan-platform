from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from endoscan_workflows.models import ArtifactRow, SourceCacheRow
from endoscan_workflows.reviewed_source_adapters import (
    ReviewedSourceExecutionError,
    ReviewedSourceOperationInput,
)
from endoscan_workflows.source_cache import SourceCacheIntegrityError
from endoscan_workflows.source_security import ScientificSourceClient
from endoscan_workflows.source_smoke import (
    IsolatedReviewedSourceSmokeRuntime,
    isolated_reviewed_source_smoke_runtime,
)
from endoscan_workflows.training_dataset import (
    ComponentRole,
    compile_verified_source_fragment,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUEST = ReviewedSourceOperationInput(query="source-neutral enzyme", maximum_results=3)
BODY = b'{"esearchresult":{"idlist":["4101"]},"marker":"RAW-SMOKE-BODY-MARKER"}'


def source_client(handler) -> ScientificSourceClient:
    return ScientificSourceClient(
        transport=httpx.MockTransport(handler),
        maximum_attempts=1,
        sleep=lambda _seconds: None,
    )


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=BODY,
        headers={"content-type": "application/json"},
        request=request,
    )


def row_counts(runtime: IsolatedReviewedSourceSmokeRuntime) -> tuple[int, int]:
    with runtime.database.session() as session:
        return (
            int(
                session.scalar(
                    select(func.count(ArtifactRow.id)).where(
                        ArtifactRow.artifact_type == "immutable_source_response"
                    )
                )
                or 0
            ),
            int(session.scalar(select(func.count(SourceCacheRow.cache_key))) or 0),
        )


def test_first_call_writes_artifact_then_cache_and_second_call_is_offline(caplog) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response(request)

    client = source_client(handler)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:
        assert runtime.database.capability()["foreign_keys"] is True
        assert runtime.artifacts.database is runtime.cache.database is runtime.database
        assert runtime.registry.readiness()["approved_adapter_count"] == 6
        assert all(
            adapter.artifacts.database is adapter.cache.database is runtime.database
            for adapter in runtime.registry.approved()
        )
        first = runtime.execute(
            "search_activity_sources",
            REQUEST,
            idempotency_key="source-neutral-first-call",
        )
        first_counts = row_counts(runtime)
        second = runtime.execute(
            "search_activity_sources",
            REQUEST,
            idempotency_key="source-neutral-second-call",
        )
        assert calls == 1
        assert first.telemetry.cache_status == "miss_written"
        assert first.telemetry.cache_write_status == "written"
        assert first.telemetry.parser_status == "succeeded"
        assert first.telemetry.http_status == 200
        assert first.telemetry.mime_type == "application/json"
        assert first.telemetry.response_bytes == len(BODY)
        assert first.telemetry.final_allowlisted_host == "eutils.ncbi.nlm.nih.gov"
        assert first.telemetry.redirect_count == 0
        assert first.telemetry.retry_count == 0
        assert first.telemetry.raw_artifact_id
        assert first.telemetry.raw_artifact_sha256
        assert first.telemetry.provenance_record_id == (f"source-cache:{first.telemetry.cache_key}")
        descriptor, content = runtime.artifacts.get(first.telemetry.raw_artifact_id)
        assert descriptor.sha256 == first.telemetry.raw_artifact_sha256
        assert content == BODY
        with runtime.database.session() as session:
            cache_row = session.get(SourceCacheRow, first.telemetry.cache_key)
            assert cache_row is not None
            assert session.get(ArtifactRow, cache_row.raw_artifact_id) is not None
        assert second.telemetry.cache_status == "hit"
        assert second.telemetry.cache_write_status == "not_applicable"
        assert second.telemetry.raw_artifact_sha256 == first.telemetry.raw_artifact_sha256
        assert second.batch.observations == first.batch.observations
        assert row_counts(runtime) == first_counts == (1, 1)
        serialized = json.dumps(first.telemetry.model_dump(mode="json"))
        assert "RAW-SMOKE-BODY-MARKER" not in serialized
        assert "RAW-SMOKE-BODY-MARKER" not in caplog.text
    client.close()


def test_epa_and_lincs_health_operations_are_technical_and_cache_offline() -> None:
    calls: list[str] = []

    def health_response(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/ctx-api/bioactivity/health":
            return httpx.Response(200, json={"status": "UP"}, request=request)
        assert request.url.path == "/entrez/eutils/einfo.fcgi"
        assert dict(request.url.params) == {"db": "gds", "retmode": "json"}
        return httpx.Response(
            200,
            json={"einforesult": {"dbinfo": [{"dbname": "gds"}]}},
            request=request,
        )

    client = source_client(health_response)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:
        epa_request = ReviewedSourceOperationInput()
        lincs_request = ReviewedSourceOperationInput()
        first_epa = runtime.execute(
            "check_epa_bioactivity_health",
            epa_request,
            idempotency_key="technical-health-epa-first",
        )
        first_lincs = runtime.execute(
            "check_lincs_geo_distribution_health",
            lincs_request,
            idempotency_key="technical-health-lincs-first",
        )
        cached_epa = runtime.execute(
            "check_epa_bioactivity_health",
            epa_request,
            idempotency_key="technical-health-epa-cached",
        )
        cached_lincs = runtime.execute(
            "check_lincs_geo_distribution_health",
            lincs_request,
            idempotency_key="technical-health-lincs-cached",
        )
        assert calls == [
            "/ctx-api/bioactivity/health",
            "/entrez/eutils/einfo.fcgi",
        ]
        assert first_epa.batch.observations == first_lincs.batch.observations == []
        assert first_epa.batch.source_request_count == first_lincs.batch.source_request_count == 1
        assert cached_epa.batch.source_request_count == cached_lincs.batch.source_request_count == 0
        assert cached_epa.telemetry.cache_status == cached_lincs.telemetry.cache_status == "hit"
        assert row_counts(runtime) == (2, 2)
    client.close()


def test_restart_reloads_cache_artifact_and_compiles_provenance_without_transport() -> None:
    with TemporaryDirectory(prefix="endoscan-smoke-restart-") as temporary:
        root = Path(temporary)
        first_client = source_client(ok_response)
        runtime = IsolatedReviewedSourceSmokeRuntime.open(
            root=root, repo_root=REPO_ROOT, client=first_client
        )
        first = runtime.execute("search_activity_sources", REQUEST, idempotency_key="restart-first")
        runtime.close()
        first_client.close()

        def forbidden_transport(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("valid cache recovery must not call the transport")

        second_client = source_client(forbidden_transport)
        restarted = IsolatedReviewedSourceSmokeRuntime.open(
            root=root, repo_root=REPO_ROOT, client=second_client
        )
        recovered = restarted.execute(
            "search_activity_sources", REQUEST, idempotency_key="restart-second"
        )
        assert recovered.telemetry.cache_status == "hit"
        descriptor, content = restarted.artifacts.get(recovered.telemetry.raw_artifact_id or "")
        assert descriptor.sha256 == recovered.telemetry.raw_artifact_sha256
        assert content == BODY
        fragment = compile_verified_source_fragment(
            fragment_id="source-neutral-fragment",
            component_roles=[ComponentRole.ENDPOINT_ACTIVITY],
            observations=recovered.batch.observations,
            review=None,
        )
        assert fragment.candidate_records[0].artifact_hashes == [
            first.telemetry.raw_artifact_sha256
        ]
        restarted.close()
        second_client.close()


def test_parser_failure_preserves_artifact_without_cache_or_transport_retry() -> None:
    calls = 0

    def malformed(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=b"{not-json",
            headers={"content-type": "application/json"},
            request=request,
        )

    client = source_client(malformed)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:
        with pytest.raises(ReviewedSourceExecutionError) as captured:
            runtime.execute("search_activity_sources", REQUEST, idempotency_key="parser-failure")
        assert calls == 1
        assert captured.value.telemetry.parser_status == "failed"
        assert captured.value.telemetry.cache_status == "miss_not_written"
        assert captured.value.telemetry.raw_artifact_id
        assert row_counts(runtime) == (1, 0)
        assert "not-json" not in str(captured.value)
    client.close()


def test_cache_failure_preserves_artifact_without_transport_retry(monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response(request)

    client = source_client(handler)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:

        def fail_cache(*_args, **_kwargs):
            raise RuntimeError("synthetic cache failure")

        monkeypatch.setattr(runtime.cache, "put", fail_cache)
        with pytest.raises(ReviewedSourceExecutionError) as captured:
            runtime.execute("search_activity_sources", REQUEST, idempotency_key="cache-failure")
        assert calls == 1
        assert captured.value.telemetry.cache_write_status == "failed"
        assert captured.value.telemetry.cache_status == "miss_not_written"
        assert row_counts(runtime) == (1, 0)
    client.close()


def test_cache_rejects_missing_or_hash_mismatched_artifact_and_fk_stays_enabled() -> None:
    client = source_client(ok_response)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:
        with pytest.raises(SourceCacheIntegrityError):
            runtime.cache.put(
                "missing-artifact",
                {},
                source_url="https://eutils.ncbi.nlm.nih.gov/",
                content_hash="0" * 64,
                parsed_output={},
                raw_artifact_id="art-missing",
                http_metadata={},
            )
        with runtime.database.session() as session:
            session.add(
                SourceCacheRow(
                    cache_key="f" * 64,
                    tool_name="invalid-direct-row",
                    normalized_arguments_json="{}",
                    policy_version="phase1-source-policy-v2-geo-text",
                    source_version=None,
                    source_url="https://eutils.ncbi.nlm.nih.gov/",
                    http_metadata_json="{}",
                    content_hash="0" * 64,
                    parsed_output_json="{}",
                    raw_artifact_id="art-missing",
                    retrieved_at="2026-01-01T00:00:00+00:00",
                    expires_at="2099-01-01T00:00:00+00:00",
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
    client.close()


def test_corrupted_cached_artifact_reference_fails_closed() -> None:
    with TemporaryDirectory(prefix="endoscan-smoke-corrupt-") as temporary:
        root = Path(temporary)
        client = source_client(ok_response)
        runtime = IsolatedReviewedSourceSmokeRuntime.open(
            root=root, repo_root=REPO_ROOT, client=client
        )
        runtime.execute("search_activity_sources", REQUEST, idempotency_key="corrupt-first")
        runtime.close()
        connection = sqlite3.connect(root / "workflow.db")
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM artifacts")
            connection.commit()
        finally:
            connection.close()
        reopened = IsolatedReviewedSourceSmokeRuntime.open(
            root=root, repo_root=REPO_ROOT, client=client
        )
        with pytest.raises(SourceCacheIntegrityError):
            reopened.execute("search_activity_sources", REQUEST, idempotency_key="corrupt-second")
        reopened.close()
        client.close()


def test_temporary_runtime_cleanup_removes_database_and_artifact_root() -> None:
    client = source_client(ok_response)
    with isolated_reviewed_source_smoke_runtime(repo_root=REPO_ROOT, client=client) as runtime:
        root = runtime.root
        runtime.execute("search_activity_sources", REQUEST, idempotency_key="cleanup-call")
        assert root.exists()
    assert not root.exists()
    client.close()
