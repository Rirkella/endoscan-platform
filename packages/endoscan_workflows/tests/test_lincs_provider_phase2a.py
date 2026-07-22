from __future__ import annotations

import gzip
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from endoscan_workflows.contracts import (
    EndpointBuildCreate,
    ToolCallStatus,
    ToolInvocation,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.discovery_strategy import (
    ArtifactReference,
    DiscoveryBudgets,
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    EvidenceRole,
    build_discovery_plan,
    initial_execution_ledger,
    load_operational_capability_registry,
)
from endoscan_workflows.lincs_metadata import (
    CompoundIdentityRecord,
    LincsCompoundMatchStatus,
    LincsContextFilter,
    LincsMetadataProvider,
    LincsMetadataRetrievalInput,
    LincsRowStatus,
    build_lincs_metadata_indexes,
    compute_lincs_metadata_coverage,
    load_lincs_release_registry,
    normalize_lincs_metadata,
    parse_lincs_metadata_resource,
)
from endoscan_workflows.models import ArtifactRow, SourceCacheRow
from endoscan_workflows.provider_execution import (
    ProviderItemKind,
    ProviderResourceRequest,
    ProviderResourceStatus,
    ProviderTaskExecutor,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificResponse, SourceTimeoutError
from endoscan_workflows.tools import ToolRegistry, extend_training_dataset_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).with_name("fixtures") / "lincs_phase2a"
RELEASE_ID = "lincs-gse92742-phase1-2017"


class FixtureTransport:
    def __init__(self, payloads: dict[str, bytes], *, failures: dict[str, int] | None = None):
        self.payloads = payloads
        self.failures = dict(failures or {})
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
        self.calls.append(url)
        if self.failures.get(url, 0):
            self.failures[url] -= 1
            raise SourceTimeoutError("Reviewed fixture source timed out.")
        content = self.payloads[url]
        assert len(content) <= maximum_bytes
        return ScientificResponse(
            url=url,
            content=content,
            content_type="application/gzip",
            status_code=200,
            headers={"content-type": "application/gzip"},
            retrieved_at=time.time(),
        )


def _payloads() -> dict[str, bytes]:
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    return {
        item.public_locator: gzip.compress(
            (FIXTURES / f"{item.logical_role}.tsv").read_bytes(), mtime=0
        )
        for item in manifest.resources
        if item.logical_role != "level5_gctx"
    }


def _workflow_id(service) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Offline LINCS metadata coverage",
            endpoint_slug="offline-lincs-metadata-coverage",
            biological_goal=(
                "Compute reviewed metadata-only compound and signature coverage without "
                "reading expression values."
            ),
            created_by="phase2a-test",
            idempotency_key="phase2a-lincs-fixture-build",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
        )
    ).id


def _record() -> DiscoveryExecutionRecord:
    return DiscoveryExecutionRecord(
        task_id="task-lincs-phase2a",
        provider="lincs-l1000",
        evidence_role=EvidenceRole.TRANSCRIPTOMIC,
        status=DiscoveryTaskStatus.PENDING,
    )


def test_reviewed_manifest_is_derived_from_sources_and_excludes_level5() -> None:
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    sources_text = (REPO_ROOT / "registry" / "data" / "sources.yaml").read_text(encoding="utf-8")
    assert {item.logical_role for item in manifest.resources} == {
        "pert_info",
        "sig_info",
        "cell_info",
        "gene_info",
        "level5_gctx",
    }
    for item in manifest.resources:
        assert item.source_registry_locator_name in sources_text
        assert item.public_locator in sources_text

    task = LincsMetadataProvider(
        load_lincs_release_registry(REPO_ROOT),
        executor=None,  # type: ignore[arg-type]
    ).retrieval_task(
        workflow_id="workflow-manifest-test",
        task_id="task-manifest-test",
        release_id=RELEASE_ID,
        idempotency_key="manifest-test",
    )
    assert {item.logical_role for item in task.resources} == {
        "pert_info",
        "sig_info",
        "cell_info",
        "gene_info",
    }
    assert all("Level5" not in item.locator for item in task.resources)
    assert task.maximum_transport_retries == 1


def test_metadata_parser_rejects_decompression_expansion_beyond_bound() -> None:
    resource = ProviderResourceRequest(
        resource_id="bounded-gzip-fixture",
        logical_role="sig_info",
        locator="https://ftp.ncbi.nlm.nih.gov/example.txt.gz",
        item_kind=ProviderItemKind.FILE,
        accepted_mime_types=["application/gzip"],
        maximum_response_bytes=1_024,
        compression="gzip",
    )
    compressed = gzip.compress(b"column\n" + (b"x" * 10_241), mtime=0)
    assert len(compressed) < resource.maximum_response_bytes
    with pytest.raises(ValueError, match="decompressed size policy"):
        parse_lincs_metadata_resource(resource, compressed)


def test_provider_executor_resumes_partial_manifest_and_replays_without_network(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow_id(service)
    manifest = load_lincs_release_registry(REPO_ROOT).release(RELEASE_ID)
    sig_url = next(
        item.public_locator for item in manifest.resources if item.logical_role == "sig_info"
    )
    transport = FixtureTransport(_payloads(), failures={sig_url: 2})
    cache = SourceResponseCache(database)
    executor = ProviderTaskExecutor(transport=transport, cache=cache, artifacts=store)
    provider = LincsMetadataProvider(load_lincs_release_registry(REPO_ROOT), executor)
    task = provider.retrieval_task(
        workflow_id=workflow_id,
        task_id="task-lincs-phase2a",
        release_id=RELEASE_ID,
        idempotency_key="phase2a-retrieval",
    )

    partial_output = provider.retrieve(
        LincsMetadataRetrievalInput(
            release_id=RELEASE_ID,
            ledger_record=_record(),
        ),
        ToolInvocation(
            tool_name="retrieve_lincs_metadata_release",
            arguments={},
            workflow_id=workflow_id,
            workflow_stage=WorkflowState.HYDRATING_SOURCE_CANDIDATES,
            idempotency_key="phase2a-retrieval",
        ),
    )
    partial = partial_output.execution
    assert partial_output.metadata is None
    assert not partial.completion_proof.completed
    assert partial.completion_proof.missing_mandatory_resource_ids == ["gse92742-sig-info"]
    assert partial.ledger_record.status is DiscoveryTaskStatus.RUNNING
    assert partial.logical_tool_call_count == 1
    assert partial.scientific_source_request_count == 4
    assert partial.transport_attempt_count == 5
    assert len([item for item in partial.page_or_file_results if item.raw_artifact]) == 3

    resumed = executor.execute(task, partial.ledger_record, parser=parse_lincs_metadata_resource)
    assert resumed.completion_proof.completed
    assert resumed.ledger_record.status is DiscoveryTaskStatus.COMPLETED
    assert resumed.logical_tool_call_count == 2
    assert resumed.scientific_source_request_count == 5
    assert resumed.transport_attempt_count == 6
    assert [item.status for item in resumed.page_or_file_results].count(
        ProviderResourceStatus.CACHE_HIT
    ) == 3
    assert len(transport.calls) == 6

    metadata = normalize_lincs_metadata(manifest, resumed)
    assert len(metadata.perturbagens) == 7
    assert len(metadata.signatures) == 7
    assert any(item.row_status is LincsRowStatus.EXCLUDED for item in metadata.perturbagens)
    assert any(item.row_status is LincsRowStatus.INCOMPLETE for item in metadata.signatures)
    assert metadata.expression_values_retrieved is False
    assert all(item.original_source_fields for item in metadata.perturbagens)

    artifact_count = len(store.list_artifacts(workflow_id))
    replayed = executor.execute(task, resumed.ledger_record, parser=parse_lincs_metadata_resource)
    assert replayed.completion_proof.completed
    assert replayed.scientific_source_request_count == 5
    assert replayed.transport_attempt_count == 6
    assert replayed.logical_tool_call_count == 3
    assert all(
        item.status is ProviderResourceStatus.CACHE_HIT for item in replayed.page_or_file_results
    )
    assert len(transport.calls) == 6
    assert len(store.list_artifacts(workflow_id)) == artifact_count
    assert replayed.cache_manifest.source_version == "phase1_2017"
    assert replayed.cache_manifest.licence_and_provenance
    assert all(item.http_status == 200 for item in replayed.page_or_file_results)
    assert all(item.final_locator for item in replayed.page_or_file_results)

    with database.session() as session:
        cache_rows = session.scalars(select(SourceCacheRow)).all()
        assert len(cache_rows) == 4
        for row in cache_rows:
            artifact = session.get(ArtifactRow, row.raw_artifact_id)
            assert artifact is not None
            assert artifact.sha256 == row.content_hash


def test_exact_indexes_all_compounds_and_context_coverage(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow_id(service)
    transport = FixtureTransport(_payloads())
    executor = ProviderTaskExecutor(
        transport=transport,
        cache=SourceResponseCache(database),
        artifacts=store,
    )
    registry = load_lincs_release_registry(REPO_ROOT)
    provider = LincsMetadataProvider(registry, executor)
    task = provider.retrieval_task(
        workflow_id=workflow_id,
        task_id="task-lincs-phase2a",
        release_id=RELEASE_ID,
        idempotency_key="phase2a-coverage",
    )
    outcome = executor.execute(task, _record(), parser=parse_lincs_metadata_resource)
    metadata = normalize_lincs_metadata(registry.release(RELEASE_ID), outcome)
    indexes = build_lincs_metadata_indexes(metadata)
    compounds = [
        CompoundIdentityRecord(
            compound_id="alpha",
            inchikey="AAAAAAAAAAAAAA-BBBBBBBBBB-C",
        ),
        CompoundIdentityRecord(compound_id="beta", pubchem_cid="CID:222"),
        CompoundIdentityRecord(compound_id="ambiguous", canonical_name="Shared exact name"),
        CompoundIdentityRecord(
            compound_id="provider",
            reviewed_provider_identifiers={"lincs-l1000": "BRD-E00000005"},
        ),
        CompoundIdentityRecord(compound_id="missing"),
    ]
    coverage = compute_lincs_metadata_coverage(
        compounds,
        metadata,
        indexes,
        contexts=[
            LincsContextFilter(context_id="a549", cell_ids=["A549"]),
            LincsContextFilter(context_id="mcf7", cell_ids=["MCF7"]),
        ],
    )
    assert coverage.total_input_compounds == 5
    assert coverage.exact_perturbagen_matches == 3
    assert coverage.ambiguous_matches == 1
    assert coverage.unmatched_compounds == 1
    assert coverage.expression_values_retrieved is False
    assert coverage.signature_counts_per_compound == {
        "alpha": 2,
        "ambiguous": 0,
        "beta": 1,
        "missing": 0,
        "provider": 1,
    }
    assert (
        next(item for item in coverage.compound_matches if item.compound_id == "ambiguous").status
        is LincsCompoundMatchStatus.AMBIGUOUS_MATCH
    )
    assert (
        next(item for item in coverage.compound_matches if item.compound_id == "missing").status
        is LincsCompoundMatchStatus.IDENTIFIER_MISSING
    )
    union_view = next(item for item in coverage.context_views if item.operator == "union")
    assert union_view.compound_ids == [
        "alpha",
        "beta",
        "provider",
    ]
    assert next(
        item for item in coverage.context_views if item.operator == "intersection"
    ).compound_ids == ["alpha"]
    assert indexes.landmark_gene_identifiers == ["101", "102"]
    assert "vehicle control" not in indexes.perturbagen_record_ids_by_exact_name


def test_lincs_capabilities_are_operational_and_heavy_access_is_preapproval_blocked(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    provider = LincsMetadataProvider(
        load_lincs_release_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=FixtureTransport(_payloads()),
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    tools = extend_training_dataset_tool_registry(ToolRegistry(), lincs_metadata_provider=provider)
    definitions = {item.name: item for item in tools.definitions()}
    assert "retrieve_lincs_metadata_release" in definitions
    assert definitions["slice_approved_lincs_level5_expression"].access_phase.value == (
        "post_approval_extraction"
    )
    prohibited = tools.invoke(
        ToolInvocation(
            tool_name="slice_approved_lincs_level5_expression",
            arguments={
                "assembly_recipe_fingerprint": "0" * 64,
                "release_id": RELEASE_ID,
                "exact_sig_ids": ["SIG-A-A549-24"],
                "exact_gene_identifiers": ["101"],
            },
            workflow_stage=WorkflowState.COMPUTING_COMBINATION_COVERAGE,
            permission_scope=["training-dataset:slice_approved_lincs_level5_expression"],
        )
    )
    assert prohibited.status is ToolCallStatus.PROHIBITED

    operational = load_operational_capability_registry(REPO_ROOT)
    plan = build_discovery_plan(
        workflow_id="workflow-lincs-capabilities",
        endpoint_identifier="example-endpoint",
        approved_specification_artifact=ArtifactReference(
            artifact_id="artifact-specification",
            sha256="0" * 64,
            artifact_type="approved_specification",
        ),
        discovery_round=0,
        biological_target="Example target",
        target_synonyms=[],
        requested_modalities=["binding"],
        registry=operational,
        budgets=DiscoveryBudgets(
            maximum_provider_invocations=10,
            maximum_tool_calls=20,
            maximum_scientific_source_requests=40,
            maximum_input_tokens=1_000,
            maximum_output_tokens=1_000,
            maximum_estimated_cost_usd=0,
            timeout_seconds=600,
            provider_retries=0,
        ),
    )
    ledger, findings = initial_execution_ledger(plan, operational)
    lincs_task = next(
        item for item in plan.provider_specific_query_tasks if item.provider == "lincs-l1000"
    )
    assert lincs_task.operation == "retrieve_lincs_metadata_release"
    assert lincs_task.query_parameters["release_id"] == RELEASE_ID
    assert not [item for item in findings.findings if item.provider == "lincs-l1000"]
    assert (
        next(item for item in ledger.records if item.provider == "lincs-l1000").status
        is DiscoveryTaskStatus.PENDING
    )
