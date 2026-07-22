from __future__ import annotations

import gzip
import hashlib
import sqlite3
import time
from pathlib import Path

from sqlalchemy import select

from endoscan_workflows.contracts import (
    EndpointBuildCreate,
    ToolAccessPhase,
    ToolInvocation,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.discovery_strategy import (
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    EvidenceRole,
)
from endoscan_workflows.lincs_metadata import (
    CompoundIdentityRecord,
    LincsContextFilter,
    LincsMetadataRetrievalInput,
    LincsNormalizedMetadataBundle,
    load_lincs_release_registry,
)
from endoscan_workflows.lincs_streaming import (
    LINCS_NORMALIZER_VERSION,
    LincsDiskCoverageInput,
    LincsDiskIndexInput,
    StreamingLincsMetadataProvider,
    _normalize_role,
    lincs_bundle_fingerprint,
    lincs_normalization_cache_identity,
)
from endoscan_workflows.models import SourceCacheRow, WorkflowEventRow
from endoscan_workflows.provider_execution import ProviderTaskExecutor
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificResponse
from endoscan_workflows.tools import ToolRegistry, extend_training_dataset_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).with_name("fixtures") / "lincs_metadata"
RELEASE_ID = "lincs-gse92742-phase1-2017"


class FixtureTransport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        tool_name: str,
        accepted_types: frozenset[str],
        maximum_bytes: int,
        maximum_attempts: int,
        approved_request_hosts: frozenset[str] | None = None,
    ) -> ScientificResponse:
        del tool_name, accepted_types, approved_request_hosts
        assert maximum_attempts == 1
        payload = self.payloads[url]
        assert len(payload) <= maximum_bytes
        self.calls.append(url)
        return ScientificResponse(
            url=url,
            content=payload,
            content_type="application/gzip",
            status_code=200,
            headers={"content-type": "application/gzip"},
            retrieved_at=time.time(),
        )


def _payloads(*, signature_count: int | None = None) -> dict[str, bytes]:
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    result = {
        resource.public_locator: gzip.compress(
            (FIXTURES / f"{resource.logical_role}.tsv").read_bytes(), mtime=0
        )
        for resource in manifest.resources
        if resource.logical_role != "level5_gctx"
    }
    if signature_count is not None:
        signature_resource = next(
            item for item in manifest.resources if item.logical_role == "sig_info"
        )
        header = (
            "sig_id\tpert_id\tpert_type\tcell_id\tpert_dose\tpert_dose_unit\t"
            "pert_time\tpert_time_unit\tdistil_ss\tdistil_id\n"
        )
        rows = (
            f"SIG-{index:07d}\tBRD-A00000001\ttrt_cp\tA549\t10\tuM\t24\th\t0.9\tD-{index}\n"
            for index in range(signature_count)
        )
        result[signature_resource.public_locator] = gzip.compress(
            (header + "".join(rows)).encode(), mtime=0
        )
    return result


def _workflow_id(service, key: str) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Streaming LINCS metadata coverage",
            endpoint_slug=f"streaming-lincs-{key}",
            biological_goal=(
                "Compute complete reviewed metadata-only coverage without expression values."
            ),
            created_by="lincs-streaming-test",
            idempotency_key=f"lincs-streaming-{key}",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
        )
    ).id


def _record() -> DiscoveryExecutionRecord:
    return DiscoveryExecutionRecord(
        task_id="task-lincs-streaming",
        provider="lincs-l1000",
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        status=DiscoveryTaskStatus.PENDING,
    )


def _invocation(workflow_id: str, tool_name: str, key: str) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        arguments={},
        workflow_id=workflow_id,
        workflow_stage=WorkflowState.HYDRATING_SOURCE_CANDIDATES,
        idempotency_key=key,
    )


def _provider(database, store, payloads: dict[str, bytes]):
    transport = FixtureTransport(payloads)
    provider = StreamingLincsMetadataProvider(
        load_lincs_release_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    return provider, transport


def test_streaming_artifacts_indexes_coverage_and_cache_replay(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 100 * 1024 * 1024
    workflow_id = _workflow_id(service, "complete")
    provider, transport = _provider(database, store, _payloads())
    request = LincsMetadataRetrievalInput(
        release_id=RELEASE_ID,
        ledger_record=_record(),
    )
    invocation = _invocation(workflow_id, "retrieve_lincs_metadata_release", "streaming")

    cold = provider.retrieve(request, invocation)
    assert cold.execution.completion_proof.completed
    assert cold.normalization_ran and cold.indexing_ran
    assert cold.normalization_manifest is not None
    assert cold.normalization_manifest_artifact is not None
    assert cold.normalization_manifest.expression_values_retrieved is False
    assert (
        lincs_bundle_fingerprint(
            provider.registry.release(RELEASE_ID),
            cold.normalization_manifest.role_artifacts,
            normalizer_version="2.0.1-test",
        )
        != cold.normalization_manifest.bundle_fingerprint
    )
    assert len(transport.calls) == 4
    assert all(
        value["rows_materialized"] is False
        for value in cold.execution.normalized_resources.values()
    )
    expected_counts = {
        "pert_info": (7, 5, 1, 1),
        "sig_info": (7, 5, 1, 1),
        "cell_info": (4, 3, 1, 0),
        "gene_info": (4, 3, 1, 0),
    }
    for role in cold.normalization_manifest.role_artifacts:
        assert (
            role.row_count,
            role.included_row_count,
            role.incomplete_row_count,
            role.excluded_row_count,
        ) == expected_counts[role.logical_role]
        descriptor, path = store.verified_path(role.normalized_artifact.artifact_id)
        assert descriptor.sha256 == role.normalized_sha256
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == (
                role.row_count
            )
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(records)")}
            if role.logical_role == "sig_info":
                assert "idx_signatures_pert_id" in indexes
                assert "idx_signatures_sig_id" in indexes
        store.verified_path(
            cold.normalization_manifest.role_manifest_artifacts[role.logical_role].artifact_id
        )

    index_result = provider.indexes(
        LincsDiskIndexInput(normalization_manifest_artifact=cold.normalization_manifest_artifact),
        _invocation(workflow_id, "build_lincs_metadata_indexes", "indexes"),
    )
    assert not index_result.index_rebuilt
    assert index_result.total_indexed_rows == 22

    compounds = [
        CompoundIdentityRecord(compound_id="alpha", inchikey="AAAAAAAAAAAAAA-BBBBBBBBBB-C"),
        CompoundIdentityRecord(compound_id="beta", pubchem_cid="CID:222"),
        CompoundIdentityRecord(
            compound_id="provider",
            reviewed_provider_identifiers={"lincs-l1000": "BRD-E00000005"},
        ),
        CompoundIdentityRecord(compound_id="ambiguous", canonical_name="Shared exact name"),
        CompoundIdentityRecord(compound_id="missing"),
    ]
    coverage_request = LincsDiskCoverageInput(
        compounds=compounds,
        normalization_manifest_artifact=cold.normalization_manifest_artifact,
        contexts=[
            LincsContextFilter(context_id="a549", cell_ids=["A549"]),
            LincsContextFilter(context_id="mcf7", cell_ids=["MCF7"]),
        ],
        preview_limit=1,
    )
    coverage = provider.coverage(
        coverage_request,
        _invocation(workflow_id, "compute_lincs_metadata_coverage", "coverage"),
    )
    assert coverage.total_input_compounds == 5
    assert coverage.exact_perturbagen_matches == 3
    assert coverage.ambiguous_matches == 1
    assert coverage.compounds_with_measured_chemical_signatures == 3
    assert coverage.exact_available_signature_count == 4
    assert len(coverage.compound_matches_preview) == 1
    assert coverage.complete_result_in_artifact
    assert coverage.expression_values_retrieved is False
    intersection = next(item for item in coverage.context_views if item.operator == "intersection")
    assert intersection.compound_count == 1
    assert intersection.compound_ids_preview == ["alpha"]

    unbounded_preview = provider.coverage(
        coverage_request.model_copy(update={"preview_limit": 100}),
        _invocation(workflow_id, "compute_lincs_metadata_coverage", "coverage-replay"),
    )
    assert unbounded_preview.coverage_artifact == coverage.coverage_artifact
    assert unbounded_preview.exact_available_signature_count == (
        coverage.exact_available_signature_count
    )
    assert len(unbounded_preview.compound_matches_preview) == 5

    artifact_count = len(store.list_artifacts(workflow_id))
    replay = provider.retrieve(
        request.model_copy(update={"ledger_record": cold.execution.ledger_record}),
        invocation,
    )
    assert not replay.normalization_ran and not replay.indexing_ran
    assert len(transport.calls) == 4
    assert len(store.list_artifacts(workflow_id)) == artifact_count
    assert replay.normalization_manifest is not None
    assert replay.normalization_manifest.bundle_fingerprint == (
        cold.normalization_manifest.bundle_fingerprint
    )
    assert [item.normalized_sha256 for item in replay.normalization_manifest.role_artifacts] == [
        item.normalized_sha256 for item in cold.normalization_manifest.role_artifacts
    ]

    with database.session() as session:
        cache_rows = session.scalars(select(SourceCacheRow)).all()
        assert len(cache_rows) == 4
        assert all(len(row.parsed_output_json) < 2_000 for row in cache_rows)
        assert all("rows_materialized" in row.parsed_output_json for row in cache_rows)
        events = session.scalars(
            select(WorkflowEventRow).where(WorkflowEventRow.workflow_id == workflow_id)
        ).all()
        assert all(len(row.payload_json) < 20_000 for row in events)


def test_large_generated_signature_stream_is_not_materialized(
    workflow_runtime, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 100 * 1024 * 1024
    workflow_id = _workflow_id(service, "large")
    provider, _transport = _provider(database, store, _payloads(signature_count=30_000))

    def reject_monolithic_dump(*args, **kwargs):
        raise AssertionError("production streaming path serialized the legacy full bundle")

    monkeypatch.setattr(LincsNormalizedMetadataBundle, "model_dump", reject_monolithic_dump)
    output = provider.retrieve(
        LincsMetadataRetrievalInput(
            release_id=RELEASE_ID,
            ledger_record=_record(),
        ),
        _invocation(workflow_id, "retrieve_lincs_metadata_release", "large"),
    )
    assert output.normalization_manifest is not None
    signatures = output.normalization_manifest.role("sig_info")
    assert signatures.row_count == 30_000
    assert signatures.included_row_count == 30_000
    assert len(output.model_dump_json()) < 100_000
    with sqlite3.connect(store.verified_path(signatures.normalized_artifact.artifact_id)[1]) as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 30_000
        assert (
            db.execute("SELECT sig_id FROM records ORDER BY sig_id DESC LIMIT 1").fetchone()[0]
            == "SIG-0029999"
        )


def test_fingerprints_are_compact_versioned_and_tool_contracts_are_streaming(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, _service = workflow_runtime
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    hashes = {role: str(index) * 64 for index, role in enumerate(("a", "b", "c", "d"), 1)}
    first = lincs_normalization_cache_identity(manifest, hashes)
    repeated = lincs_normalization_cache_identity(manifest, dict(reversed(list(hashes.items()))))
    changed_version = lincs_normalization_cache_identity(
        manifest,
        hashes,
        normalizer_version=f"{LINCS_NORMALIZER_VERSION}-changed",
    )
    assert first == repeated
    assert changed_version != first

    provider, _transport = _provider(database, store, _payloads())
    tools = extend_training_dataset_tool_registry(ToolRegistry(), lincs_metadata_provider=provider)
    definitions = {item.name: item for item in tools.definitions()}
    assert definitions["retrieve_lincs_metadata_release"].output_schema_name == (
        "LincsStreamingRetrievalOutput"
    )
    assert definitions["build_lincs_metadata_indexes"].input_schema_name == ("LincsDiskIndexInput")
    assert definitions["compute_lincs_metadata_coverage"].output_schema_name == (
        "LincsDiskCoverageOutput"
    )
    assert definitions["slice_approved_lincs_level5_expression"].access_phase is (
        ToolAccessPhase.POST_APPROVAL_EXTRACTION
    )


def test_normalized_sqlite_hash_is_stable_and_source_sensitive(tmp_path) -> None:
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    resource = next(item for item in manifest.resources if item.logical_role == "sig_info")
    raw = tmp_path / "sig.tsv.gz"
    raw.write_bytes(gzip.compress((FIXTURES / "sig_info.tsv").read_bytes(), mtime=0))
    first = tmp_path / "first.sqlite"
    repeated = tmp_path / "repeated.sqlite"
    changed = tmp_path / "changed.sqlite"
    for target in (first, repeated):
        _normalize_role(
            raw_path=raw,
            target_path=target,
            role="sig_info",
            resource=resource,
            manifest=manifest,
        )
    stable_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, repeated)]
    assert stable_hashes[0] == stable_hashes[1]

    changed_raw = tmp_path / "changed.tsv.gz"
    changed_raw.write_bytes(
        gzip.compress(
            (FIXTURES / "sig_info.tsv").read_bytes()
            + b"SIG-CHANGED\tBRD-A00000001\ttrt_cp\tA549\t1\tuM\t24\th\t1\tD\n",
            mtime=0,
        )
    )
    _normalize_role(
        raw_path=changed_raw,
        target_path=changed,
        role="sig_info",
        resource=resource,
        manifest=manifest,
    )
    assert hashlib.sha256(changed.read_bytes()).hexdigest() != stable_hashes[0]
