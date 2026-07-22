from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook

from endoscan_workflows.contracts import (
    EndpointBuildCreate,
    ToolInvocation,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.discovery_strategy import (
    DiscoveryExecutionRecord,
    DiscoveryTaskStatus,
    EvidenceRole,
)
from endoscan_workflows.preapproval_providers import (
    PreapprovalMetadataProvider,
    PreapprovalProviderReleaseManifest,
    PreapprovalProviderReleaseRegistry,
    ProviderAccessMode,
    ProviderCapabilityStatus,
    ProviderMetadataExecutionInput,
    ProviderMetadataQuery,
    ProviderRecordKind,
    ProviderResourceScope,
    ProviderResourceTemplate,
    ProviderRetrievalMode,
    load_preapproval_provider_registry,
)
from endoscan_workflows.provider_execution import (
    ProviderCredentialBoundary,
    ProviderItemKind,
    ProviderTaskExecutor,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificResponse
from endoscan_workflows.toxcast_public_activity import (
    CachedProviderFile,
    ToxCastActivityNormalizer,
    ToxCastNormalizationResult,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (
        Path(__file__).with_name("fixtures")
        / "preapproval_providers"
        / "toxcast_authoritative_shapes.json"
    ).read_text(encoding="utf-8")
)


class AuthoritativeToxCastTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.authenticated_calls = 0

    def get(
        self,
        url: str,
        *,
        tool_name: str,
        accepted_types: frozenset[str],
        maximum_bytes: int,
        maximum_attempts: int,
        approved_request_hosts: frozenset[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ScientificResponse:
        del tool_name
        assert maximum_attempts == 1
        assert accepted_types == frozenset({"application/json"})
        assert approved_request_hosts == frozenset({"clowder.edap-cluster.com", "comptox.epa.gov"})
        parsed = urlparse(url)
        if parsed.hostname == "clowder.edap-cluster.com":
            assert headers is None
            if "/spaces/" in parsed.path:
                payload = FIXTURE["release_datasets"]
            else:
                dataset_id = parsed.path.split("/")[3]
                payload = FIXTURE["dataset_files"][dataset_id]
        else:
            assert headers is not None and set(headers) == {"x-api-key"}
            self.authenticated_calls += 1
            if parsed.path.endswith("/bioactivity/assay/"):
                payload = FIXTURE["assays"]
            elif "/data/summary/search/by-aeid/" in parsed.path:
                payload = FIXTURE["summaries"][parsed.path.rsplit("/", 1)[-1]]
            else:
                payload = FIXTURE["details"][parsed.path.rsplit("/", 1)[-1]]
        content = json.dumps(payload, sort_keys=True).encode()
        assert len(content) <= maximum_bytes
        self.calls.append(url)
        return ScientificResponse(
            url=url,
            content=content,
            content_type="application/json",
            status_code=200,
            headers={"content-type": "application/json"},
            retrieved_at=time.time(),
        )


class IdenticalPayloadTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> ScientificResponse:
        self.calls.append(url)
        content = json.dumps(
            [{"id": "dataset-1", "name": "Summary_Files", "description": "release"}],
            sort_keys=True,
        ).encode()
        return ScientificResponse(
            url=url,
            content=content,
            content_type="application/json",
            status_code=200,
            headers={"content-type": "application/json"},
            retrieved_at=time.time(),
        )


def _xlsx_fixture(sheet_name: str, headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    payload = BytesIO()
    workbook.save(payload)
    workbook.close()
    return payload.getvalue()


def _public_toxcast_fixture_payloads() -> dict[str, bytes]:
    annotations = _xlsx_fixture(
        "annotations_combined",
        [
            "aid",
            "asid",
            "acid",
            "aeid",
            "assay_name",
            "assay_component_name",
            "assay_component_endpoint_name",
            "assay_component_endpoint_desc",
            "assay_source_name",
            "assay_component_target_desc",
            "assay_design_type",
            "assay_format_type",
            "assay_function_type",
            "organism",
            "signal_direction",
            "intended_target_type",
            "intended_target_family",
            "cell_viability_assay",
            "data_usability",
        ],
        [
            [
                100 + index,
                200 + index,
                300 + index,
                400 + index,
                f"assay-{index}",
                f"component-{index}",
                f"Example_binding_{index}",
                "receptor binding",
                "ToxCast fixture",
                "example receptor",
                "binding",
                "cell-free",
                "binding",
                "human",
                "loss",
                "receptor",
                "nuclear receptor",
                0,
                1,
            ]
            for index in range(1, 7)
        ],
    )
    targets = _xlsx_fixture(
        "Sheet 1",
        [
            "aeid",
            "assay_component_endpoint_name.x",
            "assay_component_endpoint_name.y",
            "target_id",
            "target_type",
            "official_full_name",
            "official_symbol",
            "ncbi_taxon_id",
        ],
        [
            [
                400 + index,
                f"Example_binding_{index}",
                f"Example_binding_{index}",
                9000,
                "entrez_gene_id",
                "example receptor",
                "EXR",
                9606,
            ]
            for index in range(1, 7)
        ],
    )
    analytical_qc = _xlsx_fixture(
        "Sheet 1",
        [
            "analytical_qc_id",
            "dsstox_substance_id",
            "chnm",
            "spid",
            "qc_level",
            "pass_or_caution",
            "created_date",
        ],
        [
            [1, "DTXSID001", "Compound One", "SPID001", 1, "PASS", datetime(2025, 8, 1)],
            [2, "DTXSID002", "Compound Two", "SPID002", 1, "CAUTION", datetime(2025, 8, 1)],
        ],
    )
    cytotox = _xlsx_fixture(
        "Sheet 1",
        ["chid", "casn", "chnm", "dsstox_substance_id", "ntested", "nhit"],
        [[10, "1-00-0", "Compound Three", "DTXSID003", 12, 1]],
    )
    return {
        "68af6bd3e4b02565fc7c3aa8": annotations,
        "68af6bd3e4b02565fc7c3aa0": targets,
        "68af6bd3e4b02565fc7c3ab8": analytical_qc,
        "68af6bd3e4b02565fc7c3aa4": cytotox,
    }


class PublicReleaseToxCastTransport:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
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
        headers: dict[str, str] | None = None,
    ) -> ScientificResponse:
        del tool_name
        assert maximum_attempts == 1
        assert headers is None
        assert approved_request_hosts == frozenset({"clowder.edap-cluster.com", "comptox.epa.gov"})
        parsed = urlparse(url)
        assert parsed.hostname == "clowder.edap-cluster.com"
        if parsed.path.endswith("/metadata.jsonld"):
            payload: object = [
                {
                    "content": {
                        "inventory size": 3,
                        "inventory": [
                            "mc4_all_model_fits_invitrodbv4_3_AUG2024.csv",
                            "mc5-6_winning_model_fits-flags_invitrodbv4_3_AUG2024.csv",
                            "sc1_sc2_invitrodb_v4_3_AUG2024.xlsx",
                        ],
                        "sha256": "c" * 64,
                    }
                }
            ]
            content = json.dumps(payload, sort_keys=True).encode()
            content_type = "application/json"
        elif "/api/spaces/" in parsed.path:
            payload = [
                {"id": "687e392ae4b02565bc3e2914", "name": "Summary_Files"},
                {"id": "687e3940e4b02565bc3e291e", "name": "MySQL_Data"},
                {"id": "6894f2dae4b025654d12b716", "name": "Assay Description Documents"},
                {"id": "687e3921e4b02565bc3e290f", "name": "Release_Note"},
                {"id": "687e3932e4b02565bc3e2919", "name": "Plots"},
            ]
            content = json.dumps(payload, sort_keys=True).encode()
            content_type = "application/json"
        elif "/api/datasets/" in parsed.path:
            dataset_id = parsed.path.split("/")[3]
            files = {
                "687e392ae4b02565bc3e2914": [
                    {
                        "id": "68af6b70e4b02565fc7c3a98",
                        "filename": "INVITRODB_SUMMARY.zip",
                        "size": 7502075199,
                    }
                ],
                "687e3940e4b02565bc3e291e": [
                    {
                        "id": "68c3365ce4b02565fc7cd3f3",
                        "filename": "invitrodb_v4_3.sql.gz",
                        "size": 17651807605,
                    }
                ],
                "6894f2dae4b025654d12b716": [
                    {"id": "assay-doc", "filename": "Assay Endpoint ID 1.pdf", "size": 1000}
                ],
                "687e3921e4b02565bc3e290f": [
                    {
                        "id": "release-note",
                        "filename": "invitrodb_v4_3_release_note.html",
                        "size": 2000,
                    }
                ],
            }
            content = json.dumps(files[dataset_id], sort_keys=True).encode()
            content_type = "application/json"
        else:
            file_id = parsed.path.split("/")[2]
            content = self.blobs[file_id]
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert content_type in accepted_types
        assert len(content) <= maximum_bytes
        self.calls.append(url)
        return ScientificResponse(
            url=url,
            content=content,
            content_type=content_type,
            status_code=200,
            headers={"content-type": content_type},
            retrieved_at=time.time(),
        )


def _public_fixture_registry(blobs: dict[str, bytes]) -> PreapprovalProviderReleaseRegistry:
    production = load_preapproval_provider_registry(REPO_ROOT).release(
        "toxcast", "invitrodb-v4.3-2025-08"
    )
    payload = production.model_dump(mode="json", exclude={"manifest_fingerprint"})
    for resource in payload["resources"]:
        file_id = resource["locator_template"].split("/")[-2]
        if file_id in blobs:
            resource["expected_sha256"] = hashlib.sha256(blobs[file_id]).hexdigest()
    return PreapprovalProviderReleaseRegistry(
        releases=[PreapprovalProviderReleaseManifest.create(**payload)]
    )


def _workflow(service, suffix: str) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name=f"ToxCast provider contract {suffix}",
            endpoint_slug=f"toxcast-provider-contract-{suffix}",
            biological_goal="Validate authoritative provider metadata contracts only.",
            created_by="preapproval-test",
            idempotency_key=f"toxcast-provider-contract-{suffix}",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
        )
    ).id


def _stage_normalized_activity_fixture(root: Path) -> None:
    release_root = root / "toxcast" / "invitrodb-v4.3-2025-08"
    release_root.mkdir(parents=True)
    sqlite_path = release_root / "toxcast-activity-fixture.sqlite"
    with sqlite3.connect(sqlite_path) as connection:
        ToxCastActivityNormalizer._create_schema(connection)
        connection.execute(
            """
            INSERT INTO multi_concentration_hit_calls (
                dtxsid, source_chemical_id, aeid, tested_status, source_hit_call,
                activity_value, potency_value, flags_json, activity_source_type,
                original_fields_json, source_member_sha256, release_version
            ) VALUES ('DTXSID001', 'SPID001', '401', 'tested', '1', '0.8',
                      '0.4', '{}', 'multi_concentration', '{}', ?, 'invitrodb v4.3')
            """,
            ("1" * 64,),
        )
        connection.execute(
            """
            INSERT INTO single_concentration_activity (
                dtxsid, source_chemical_id, aeid, source_hit_call,
                tested_concentration, activity_value, flags_json,
                activity_source_type, original_fields_json,
                source_member_sha256, release_version
            ) VALUES ('DTXSID002', 'SPID002', '402', '0', '10', '0.1', '{}',
                      'single_concentration', '{}', ?, 'invitrodb v4.3')
            """,
            ("2" * 64,),
        )
        ToxCastActivityNormalizer._create_indexes(connection)
        connection.commit()
    sqlite_bytes = sqlite_path.read_bytes()
    sqlite_sha = hashlib.sha256(sqlite_bytes).hexdigest()
    bundle_path = release_root / "toxcast-normalization-fixture.json"
    bundle_path.write_text('{"fixture":true}\n', encoding="utf-8")
    bundle_bytes = bundle_path.read_bytes()
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
    relative_root = Path("toxcast") / "invitrodb-v4.3-2025-08"
    result = ToxCastNormalizationResult(
        sqlite=CachedProviderFile(
            cache_relative_path=(relative_root / sqlite_path.name).as_posix(),
            sha256=sqlite_sha,
            size_bytes=len(sqlite_bytes),
        ),
        manifest=CachedProviderFile(
            cache_relative_path=(relative_root / bundle_path.name).as_posix(),
            sha256=bundle_sha,
            size_bytes=len(bundle_bytes),
        ),
        tested_chemical_rows=2,
        tested_chemical_reference_rows=2,
        multi_concentration_model_fit_rows=0,
        multi_concentration_hit_call_rows=1,
        single_concentration_rows=1,
        index_names=ToxCastActivityNormalizer.INDEX_NAMES,
        input_member_hashes={"fixture": "3" * 64},
        normalization_ran=True,
        bundle_fingerprint="4" * 64,
    )
    (release_root / "normalized-activity-manifest.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def _record(task_id: str = "task-toxcast-authoritative") -> DiscoveryExecutionRecord:
    return DiscoveryExecutionRecord(
        task_id=task_id,
        provider="toxcast",
        evidence_role=EvidenceRole.ACTIVITY,
        modality="binding",
        status=DiscoveryTaskStatus.PENDING,
    )


def _invocation(workflow_id: str, suffix: str) -> ToolInvocation:
    return ToolInvocation(
        tool_name="retrieve_toxcast_preapproval_metadata",
        arguments={},
        workflow_id=workflow_id,
        workflow_stage=WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
        idempotency_key=f"toxcast-authoritative:{suffix}",
    )


def _request() -> ProviderMetadataExecutionInput:
    return ProviderMetadataExecutionInput(
        ledger_record=_record(),
        release_id="invitrodb-v4.3-2025-08",
        query=ProviderMetadataQuery(
            biological_target="example receptor",
            modality="binding",
        ),
        validation_scope="bounded_smoke",
        access_mode=ProviderAccessMode.AUTHENTICATED_API,
        maximum_summary_aeids=2,
        maximum_detail_aeids=1,
    )


def test_public_release_requires_no_key_preserves_gaps_and_replays_without_requests(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "public-release")
    blobs = _public_toxcast_fixture_payloads()
    transport = PublicReleaseToxCastTransport(blobs)
    provider = PreapprovalMetadataProvider(
        _public_fixture_registry(blobs),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    request = ProviderMetadataExecutionInput(
        ledger_record=_record("task-toxcast-public-release"),
        release_id="invitrodb-v4.3-2025-08",
        query=ProviderMetadataQuery(
            biological_target="example receptor",
            modality="binding",
        ),
        validation_scope="bounded_smoke",
    )

    output = provider.execute("toxcast", request, _invocation(workflow_id, "public-release"))

    assert output.completion_proof.completed
    assert output.access_mode is ProviderAccessMode.PUBLIC_RELEASE
    assert not output.optional_authenticated_api_available
    assert output.ledger_record.error_classification is None
    assert len(transport.calls) == 10
    assert all("comptox.epa.gov" not in url for url in transport.calls)
    assert all("pubchem" not in url.casefold() for url in transport.calls)
    assert not any(url.endswith("/68af6b70e4b02565fc7c3a98/blob") for url in transport.calls)
    assert not any(url.endswith("/68c3365ce4b02565fc7cd3f3/blob") for url in transport.calls)
    assert output.dataset_manifest is not None
    assert output.dataset_manifest.access_mode is ProviderAccessMode.PUBLIC_RELEASE
    assert output.dataset_manifest.counts.assay_annotations == 6
    assert output.dataset_manifest.counts.compound_mappings == 3
    assert output.dataset_manifest.counts.activity_records == 0
    assert output.dataset_manifest.counts.release_inventory_records == 10
    findings = {item.capability: item for item in output.capability_findings}
    assert findings["complete_assay_endpoint_inventory"].status == "operational"
    assert findings["complete_chemical_catalogue"].status is ProviderCapabilityStatus.OPERATIONAL
    assert findings["compound_level_activity_hit_calls"].status is (
        ProviderCapabilityStatus.OPERATIONAL
    )
    assert findings["source_defined_counterscreen_roles"].status is (
        ProviderCapabilityStatus.OPERATIONAL
    )
    assert all(item.finding_code is None for item in findings.values())
    assert "PROVIDER_AUTHENTICATION_REQUIRED" not in {
        item.finding_code for item in output.capability_findings
    }
    _, sqlite_path = store.verified_path(output.dataset_manifest.sqlite_artifact.artifact_id)
    with sqlite3.connect(sqlite_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_records WHERE record_kind='release'"
        ).fetchone() == (10,)
        assert connection.execute(
            "SELECT COUNT(*) FROM source_candidates WHERE source_identifier LIKE '%Summary_Files%'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM activity_records").fetchone() == (0,)

    calls_before_replay = list(transport.calls)
    replay = provider.execute("toxcast", request, _invocation(workflow_id, "public-release"))
    assert replay.cache_only_replay
    assert not replay.normalization_ran
    assert transport.calls == calls_before_replay
    assert replay.dataset_manifest.bundle_fingerprint == (
        output.dataset_manifest.bundle_fingerprint
    )


def test_public_release_connects_staged_activity_cache_and_replays(
    workflow_runtime, tmp_path: Path
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "public-activity-cache")
    blobs = _public_toxcast_fixture_payloads()
    transport = PublicReleaseToxCastTransport(blobs)
    provider_cache = tmp_path / "provider-cache"
    _stage_normalized_activity_fixture(provider_cache)
    provider = PreapprovalMetadataProvider(
        _public_fixture_registry(blobs),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
        public_provider_cache_root=provider_cache,
    )
    request = ProviderMetadataExecutionInput(
        ledger_record=_record("task-toxcast-public-activity-cache"),
        release_id="invitrodb-v4.3-2025-08",
        query=ProviderMetadataQuery(
            biological_target="example receptor",
            modality="binding",
        ),
        validation_scope="bounded_smoke",
    )

    output = provider.execute("toxcast", request, _invocation(workflow_id, "public-activity-cache"))

    assert output.completion_proof.completed
    assert output.toxcast_public_activity is not None
    assert output.toxcast_public_activity_artifact is not None
    assert output.toxcast_public_activity.matched_aeids == [
        "401",
        "402",
        "403",
        "404",
        "405",
        "406",
    ]
    coverage = output.toxcast_public_activity.coverage
    assert coverage.multi_concentration_rows == 1
    assert coverage.single_concentration_rows == 1
    assert coverage.unique_tested_compounds == 2
    assert coverage.compounds_with_stable_identifiers == 2
    calls_before = list(transport.calls)

    replay = provider.execute("toxcast", request, _invocation(workflow_id, "public-activity-cache"))

    assert replay.cache_only_replay
    assert replay.toxcast_public_activity == output.toxcast_public_activity
    assert replay.toxcast_public_activity_artifact == output.toxcast_public_activity_artifact
    assert transport.calls == calls_before


def test_authoritative_toxcast_contract_normalizes_real_shapes_and_replays(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 30 * 1024 * 1024
    workflow_id = _workflow(service, "complete")
    transport = AuthoritativeToxCastTransport()
    provider = PreapprovalMetadataProvider(
        load_preapproval_provider_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
            credential_boundary=ProviderCredentialBoundary(
                epa_comptox_api_key="offline-test-secret"
            ),
        ),
    )

    output = provider.execute("toxcast", _request(), _invocation(workflow_id, "complete"))

    assert output.completion_proof.completed
    assert output.completion_proof.validation_scope == "bounded_smoke"
    assert output.completion_proof.matched_aeid_count == 2
    assert output.completion_proof.summary_aeid_count == 2
    assert output.completion_proof.detail_aeid_count == 1
    assert output.completion_proof.structural_validation_codes == []
    assert len(transport.calls) == 4
    assert transport.authenticated_calls == 4
    assert all("role=" not in url for url in transport.calls)
    assert not any("68af6b70e4b02565fc7c3a98" in url for url in transport.calls)
    assert not any("68c3365ce4b02565fc7cd3f3" in url for url in transport.calls)
    assert output.dataset_manifest is not None
    assert output.dataset_manifest.counts.assay_annotations == 7
    assert output.dataset_manifest.counts.activity_summaries == 2
    assert output.dataset_manifest.counts.activity_records == 3
    assert output.dataset_manifest.counts.release_inventory_records == 0
    assert output.dataset_manifest.provider_provenance_partitions == {
        "tox21": 1,
        "toxcast_source_backed": 5,
        "unclassified_missing_source": 1,
    }
    assert set(output.dataset_manifest.operation_hashes) == {
        "all_assays",
        "details_by_aeid",
        "summary_by_aeid",
    }
    _, sqlite_path = store.verified_path(output.dataset_manifest.sqlite_artifact.artifact_id)
    with sqlite3.connect(sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assay_annotations").fetchone() == (7,)
        assert {
            row[0] for row in connection.execute("SELECT activity_call FROM activity_records")
        } == {"0", "1"}
        assert connection.execute(
            "SELECT COUNT(*) FROM activity_records WHERE compound_identifier LIKE 'AEID:%'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM activity_records WHERE compound_identifier LIKE 'AID:%'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM activity_records WHERE compound_identifier LIKE 'DTXSID:%'"
        ).fetchone() == (2,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM assay_relationships
            WHERE relationship_type='linked_to_pubchem_assay'
            """
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM source_candidates WHERE source_identifier LIKE 'Summary_Files%'"
        ).fetchone() == (0,)
    raw_hashes = {item.sha256 for item in output.dataset_manifest.raw_artifacts}
    assert output.dataset_manifest.sqlite_artifact.sha256 not in raw_hashes

    calls_before_replay = list(transport.calls)
    replay = provider.execute("toxcast", _request(), _invocation(workflow_id, "complete"))
    assert replay.cache_only_replay
    assert not replay.normalization_ran
    assert transport.calls == calls_before_replay
    assert replay.dataset_manifest.bundle_fingerprint == (
        output.dataset_manifest.bundle_fingerprint
    )
    for descriptor_path in store.root.rglob("*.json"):
        assert "offline-test-secret" not in descriptor_path.read_text(
            encoding="utf-8", errors="ignore"
        )


def test_explicit_authenticated_mode_without_key_never_calls_scientific_api(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "missing-auth")
    transport = AuthoritativeToxCastTransport()
    provider = PreapprovalMetadataProvider(
        load_preapproval_provider_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )

    output = provider.execute("toxcast", _request(), _invocation(workflow_id, "missing-auth"))

    assert not output.completion_proof.completed
    assert output.ledger_record.error_classification == "PROVIDER_AUTHENTICATION_REQUIRED"
    assert len(transport.calls) == 0
    assert transport.authenticated_calls == 0
    assert output.ledger_record.scientific_source_request_count == 0
    assert output.dataset_manifest is None
    assert not output.normalization_ran


def test_production_full_processes_every_deterministically_matched_aeid(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 30 * 1024 * 1024
    workflow_id = _workflow(service, "production-full")
    transport = AuthoritativeToxCastTransport()
    provider = PreapprovalMetadataProvider(
        load_preapproval_provider_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
            credential_boundary=ProviderCredentialBoundary(
                epa_comptox_api_key="offline-test-secret"
            ),
        ),
    )
    request = ProviderMetadataExecutionInput(
        ledger_record=_record("task-toxcast-production-full"),
        release_id="invitrodb-v4.3-2025-08",
        query=ProviderMetadataQuery(
            biological_target="example receptor",
            modality="binding",
        ),
        access_mode=ProviderAccessMode.AUTHENTICATED_API,
    )

    output = provider.execute("toxcast", request, _invocation(workflow_id, "production-full"))

    assert output.completion_proof.completed
    assert output.completion_proof.validation_scope == "production_full"
    assert output.completion_proof.matched_aeid_count == 2
    assert output.completion_proof.summary_aeid_count == 2
    assert output.completion_proof.detail_aeid_count == 2
    assert output.dataset_manifest.counts.activity_records == 4
    assert len(transport.calls) == 5


def test_identical_mandatory_scientific_roles_stop_before_normalization(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "aliasing")
    host = "fixture.invalid"
    resources = [
        ProviderResourceTemplate(
            resource_id=f"role-{index}",
            logical_role=role,
            record_kind=kind,
            locator_template=f"https://{host}/resource/{index}",
            item_kind=ProviderItemKind.FILE,
            accepted_mime_types=["application/json"],
            maximum_response_bytes=100_000,
            operation_id=f"operation-{index}",
            output_schema=f"Schema{index}[]",
            resource_scope=ProviderResourceScope.SCIENTIFIC_DATA,
        )
        for index, (role, kind) in enumerate(
            (
                ("chemical_catalogue", ProviderRecordKind.COMPOUND),
                ("assay_catalogue", ProviderRecordKind.ASSAY),
                ("activity_catalogue", ProviderRecordKind.ACTIVITY),
            ),
            start=1,
        )
    ]
    manifest = PreapprovalProviderReleaseManifest.create(
        release_id="fixture-alias-v1",
        provider="toxcast",
        adapter_id="toxcast-alias-fixture",
        adapter_version="2.0.0",
        source_system="offline alias fixture",
        source_version="fixture-v1",
        source_locator=f"https://{host}/release",
        reviewed_hosts=[host],
        retrieval_mode=ProviderRetrievalMode.MANIFEST_FILES,
        resources=resources,
        licence_and_provenance=["offline identical response fixture"],
        release_checksum_or_citation="fixture SHA-256",
    )
    provider = PreapprovalMetadataProvider(
        PreapprovalProviderReleaseRegistry(releases=[manifest]),
        ProviderTaskExecutor(
            transport=IdenticalPayloadTransport(),
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    request = ProviderMetadataExecutionInput(
        ledger_record=_record("task-toxcast-alias"),
        release_id=manifest.release_id,
        query=ProviderMetadataQuery(biological_target="example receptor"),
    )

    output = provider.execute("toxcast", request, _invocation(workflow_id, "aliasing"))

    assert not output.completion_proof.completed
    assert output.completion_proof.structural_validation_codes == [
        "ROLE_ENDPOINT_ALIASING_DETECTED"
    ]
    assert output.ledger_record.error_classification == "ROLE_ENDPOINT_ALIASING_DETECTED"
    assert output.dataset_manifest is None
    assert not output.normalization_ran


def test_release_space_listing_cannot_satisfy_scientific_role(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "release-as-science")
    host = "fixture.invalid"
    resource = ProviderResourceTemplate(
        resource_id="assay-catalogue",
        logical_role="assay_catalogue",
        record_kind=ProviderRecordKind.ASSAY,
        locator_template=(f"https://{host}/api/spaces/687e388ce4b02565bc3e28e4/datasets"),
        item_kind=ProviderItemKind.FILE,
        accepted_mime_types=["application/json"],
        maximum_response_bytes=100_000,
        operation_id="unregistered_assay_catalogue",
        output_schema="AssayAnnotation[]",
        resource_scope=ProviderResourceScope.SCIENTIFIC_DATA,
    )
    manifest = PreapprovalProviderReleaseManifest.create(
        release_id="fixture-release-as-science-v1",
        provider="toxcast",
        adapter_id="toxcast-release-as-science-fixture",
        adapter_version="2.0.0",
        source_system="offline release-list fixture",
        source_version="fixture-v1",
        source_locator=f"https://{host}/release",
        reviewed_hosts=[host],
        retrieval_mode=ProviderRetrievalMode.MANIFEST_FILES,
        resources=[resource],
        licence_and_provenance=["offline release listing fixture"],
        release_checksum_or_citation="fixture SHA-256",
    )
    provider = PreapprovalMetadataProvider(
        PreapprovalProviderReleaseRegistry(releases=[manifest]),
        ProviderTaskExecutor(
            transport=IdenticalPayloadTransport(),
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    request = ProviderMetadataExecutionInput(
        ledger_record=_record("task-toxcast-release-as-science"),
        release_id=manifest.release_id,
        query=ProviderMetadataQuery(biological_target="example receptor"),
    )

    output = provider.execute("toxcast", request, _invocation(workflow_id, "release-as-science"))

    assert output.completion_proof.structural_validation_codes == [
        "RELEASE_LIST_CANNOT_SATISFY_SCIENTIFIC_ROLE"
    ]
    assert output.dataset_manifest is None
    assert not output.normalization_ran


def test_production_manifest_separates_release_api_and_bulk_contracts() -> None:
    manifest = load_preapproval_provider_registry(REPO_ROOT).release(
        "toxcast", "invitrodb-v4.3-2025-08"
    )
    assert manifest.api_contract is not None
    assert manifest.api_contract.openapi_version == "3.1.0"
    assert manifest.api_contract.api_version == "1.1.1"
    assert manifest.api_contract.authentication_scheme == "x-api-key"
    assert manifest.release_authority.release_space_id == "687e388ce4b02565bc3e28e4"
    assert all("role=" not in item.locator_template for item in manifest.resources)
    assert all(not item.automatic_download for item in manifest.bulk_fallback_resources)
    assert {item.resource_id for item in manifest.bulk_fallback_resources} == {
        "invitrodb_summary_zip",
        "invitrodb_mysql_gzip",
    }
    public_scientific = [
        item
        for item in manifest.resources
        if item.resource_scope == "scientific_data"
        and item.access_mode == ProviderAccessMode.PUBLIC_RELEASE
    ]
    authenticated_scientific = [
        item
        for item in manifest.resources
        if item.resource_scope == "scientific_data"
        and item.access_mode == ProviderAccessMode.AUTHENTICATED_API
    ]
    assert {item.operation_id for item in public_scientific} == {
        "public_assay_annotations",
        "public_assay_target_mappings",
        "public_chemical_analytical_qc",
        "public_cytotox_reference",
    }
    assert all(item.required_credential is None for item in public_scientific)
    assert {item.operation_id for item in authenticated_scientific} == {
        "all_assays",
        "summary_by_aeid",
        "details_by_aeid",
    }
    assert all(
        item.required_credential == "epa_comptox_api_key" for item in authenticated_scientific
    )
