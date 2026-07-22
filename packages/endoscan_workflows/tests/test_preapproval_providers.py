from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from endoscan_workflows.contracts import (
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
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
    OperationalCapability,
    OperationalCapabilityRegistry,
    ProviderOperationalProfile,
    build_discovery_plan,
    initial_execution_ledger,
    load_operational_capability_registry,
)
from endoscan_workflows.errors import StaleWorkflowVersion, WorkflowConflict
from endoscan_workflows.preapproval_providers import (
    PROVIDER_TOOL_NAMES,
    PreapprovalMetadataProvider,
    PreapprovalProviderLayer,
    PreapprovalProviderReleaseManifest,
    PreapprovalProviderReleaseRegistry,
    ProviderMetadataExecutionInput,
    ProviderMetadataQuery,
    ProviderRecordKind,
    ProviderResourceTemplate,
    ProviderRetrievalMode,
    _render_locator,
    compile_provider_metadata_input,
    load_preapproval_provider_registry,
)
from endoscan_workflows.provider_execution import ProviderItemKind, ProviderTaskExecutor
from endoscan_workflows.semantics_v2_executor import (
    PROVIDER_OPERATIONS,
    DiscoveryTaskMaterialization,
    SemanticsV2DiscoveryExecutor,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificResponse, SourcePolicyError
from endoscan_workflows.tools import ToolRegistry, extend_training_dataset_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (
        Path(__file__).with_name("fixtures")
        / "preapproval_providers"
        / "semantics_v2_providers.json"
    ).read_text(encoding="utf-8")
)
HOST = "fixture.ncbi.invalid"


class FixtureTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.approved_request_hosts: list[frozenset[str] | None] = []

    def _payload(self, url: str) -> dict:
        path = urlparse(url).path.strip("/").split("/")
        provider = path[0]
        token = unquote(path[-1])
        if provider == "toxcast":
            key = {
                "release": "release",
                "compounds": "compounds",
                "assays": "assays",
                "activity": "activity",
            }[token]
            return {"records": FIXTURE["toxcast"][key], "terminal": True}
        if provider == "pubchem-compound":
            requested = token.split(",")
            records = [
                item
                for item in FIXTURE["pubchem_compounds"]
                if item["source_identifier"] in requested
            ]
            return {"records": records, "terminal": True}
        page = int(token)
        if provider == "tox21":
            return FIXTURE["tox21_pages"][page // 3]
        if provider == "pubchem-bioassay":
            return FIXTURE["pubchem_bioassay_pages"][page // 3]
        if provider == "ncbi-geo":
            return FIXTURE["geo_pages"][page // 3]
        if provider == "ncbi-supporting-metadata":
            return FIXTURE["supporting_pages"][page // 3]
        raise AssertionError(url)

    def get(
        self,
        url: str,
        *,
        tool_name: str,
        accepted_types: frozenset[str],
        maximum_bytes: int,
        maximum_attempts: int,
        approved_request_hosts: frozenset[str] | None = None,
        allow_official_geo_text: bool = False,
    ) -> ScientificResponse:
        del tool_name
        assert maximum_attempts == 1
        assert "application/json" in accepted_types
        payload = json.dumps(self._payload(url), sort_keys=True).encode()
        assert len(payload) <= maximum_bytes
        self.calls.append(url)
        self.approved_request_hosts.append(approved_request_hosts)
        if "geo/query/acc.cgi" in url:
            assert allow_official_geo_text
        return ScientificResponse(
            url=url,
            content=payload,
            content_type="application/json",
            status_code=200,
            headers={"content-type": "application/json"},
            retrieved_at=time.time(),
        )


class ModalityAwareFixtureTransport(FixtureTransport):
    def _payload(self, url: str) -> dict:
        payload = json.loads(json.dumps(super()._payload(url)))
        query = unquote(parse_qs(urlparse(url).query).get("query", [""])[0]).casefold()
        requested_modality = query.rsplit(" and ", maxsplit=1)[-1]
        modality = (
            requested_modality
            if requested_modality in {"binding", "agonism", "antagonism"}
            else None
        )
        if modality is not None:
            for record in payload.get("records", []):
                if record.get("record_kind") in {"assay", "activity"}:
                    record["modality"] = modality
        return payload


class ArbitraryCompoundTransport(FixtureTransport):
    def _payload(self, url: str) -> dict:
        token = unquote(urlparse(url).path.strip("/").split("/")[-1])
        return {
            "records": [
                {
                    "record_kind": "compound",
                    "source_identifier": identifier,
                    "compound_identifier": identifier,
                    "cid": identifier.removeprefix("CID:"),
                    "canonical_name": f"Compound {index}",
                    "mapping_status": "exact",
                    "structure_status": "single_structure",
                    "provenance": ["generated offline full-set fixture"],
                }
                for index, identifier in enumerate(token.split(","), start=1)
            ],
            "terminal": True,
        }


class LocalPolicyFailureTransport(FixtureTransport):
    def get(self, url: str, **kwargs):
        self.calls.append(url)
        self.approved_request_hosts.append(kwargs.get("approved_request_hosts"))
        raise SourcePolicyError("Reviewed fixture request failed local source policy.")


class HydrationTransport:
    """Official-shape transport for complete search -> hydrate -> replay regressions."""

    def __init__(self) -> None:
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
        allow_official_geo_text: bool = False,
    ) -> ScientificResponse:
        del tool_name, accepted_types, approved_request_hosts
        assert maximum_attempts == 1
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/geo/query/acc.cgi"):
            assert allow_official_geo_text
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        content_type = "application/json"
        if parsed.path.endswith("esearch.fcgi"):
            database = query["db"]
            term = unquote(query["term"]).casefold()
            identifiers = (
                ["1001", "1002"]
                if database == "pcassay" and "tox21" in term
                else ["2001", "2002"]
                if database == "pcassay"
                else ["3001", "3002"]
            )
            payload: object = {
                "esearchresult": {
                    "count": str(len(identifiers)),
                    "retstart": query.get("retstart", "0"),
                    "idlist": identifiers,
                }
            }
            content = json.dumps(payload).encode()
        elif parsed.path.endswith("esummary.fcgi"):
            identifiers = query["id"].split(",")
            if query["db"] == "pcassay":
                payload = {
                    "result": {
                        "uids": identifiers,
                        **{
                            identifier: {
                                "uid": identifier,
                                "title": f"Example receptor assay {identifier}",
                                "targetname": "Example receptor",
                                "description": "Source reviewed binding assay",
                                "assaytype": "biochemical binding assay",
                                "activityoutcome": "active/inactive",
                                "organism": "Homo sapiens",
                            }
                            for identifier in identifiers
                        },
                    }
                }
            else:
                payload = {
                    "result": {
                        "uids": identifiers,
                        **{
                            identifier: {
                                "uid": identifier,
                                "accession": f"GSE{identifier}",
                                "title": "Example compound transcriptomic profile",
                                "summary": (
                                    "Chemical treatment at 10 uM for 24 hours with controls "
                                    "and gene expression profiling."
                                ),
                                "taxon": "Homo sapiens",
                                "gdsType": "A549 cells",
                                "suppfile": "processed.tsv.gz",
                                "ftpLink": f"https://ftp.ncbi.nlm.nih.gov/geo/GSE{identifier}",
                            }
                            for identifier in identifiers
                        },
                    }
                }
            content = json.dumps(payload).encode()
        elif "/rest/pug/assay/aid/" in parsed.path and parsed.path.endswith("/cids/JSON"):
            aid = parsed.path.split("/aid/")[1].split("/")[0]
            content = json.dumps({"IdentifierList": {"CID": [int(aid) + 10_000]}}).encode()
        elif "/rest/pug/assay/aid/" in parsed.path and parsed.path.endswith("/concise/CSV"):
            aid = parsed.path.split("/aid/")[1].split("/")[0]
            content_type = "text/csv"
            content = (
                "PUBCHEM_RESULT_TAG,PUBCHEM_SID,PUBCHEM_CID,"
                "PUBCHEM_ACTIVITY_OUTCOME,AC50 [uM]\n"
                f"1,{int(aid) + 70000},{int(aid) + 10000},Active,0.5\n"
                f"2,{int(aid) + 80000},{int(aid) + 20000},Inactive,10\n"
            ).encode()
        elif parsed.path.endswith("/description/JSON"):
            aid = parsed.path.split("/aid/")[1].split("/")[0]
            content = json.dumps(
                {
                    "PC_AssayContainer": [
                        {
                            "assay": {
                                "descr": {
                                    "aid": {"id": int(aid), "version": 1},
                                    "xref": [
                                        {
                                            "xref": {"aid": int(aid) + 1},
                                            "comment": "Source-declared counter screen",
                                        }
                                    ],
                                }
                            }
                        }
                    ]
                }
            ).encode()
        elif parsed.path.endswith("acc.cgi"):
            accession = query["acc"]
            content_type = "text/plain"
            content = (
                f"^SERIES = {accession}\n"
                f"!Series_geo_accession = {accession}\n"
                "!Series_title = Example compound gene expression study\n"
                "!Series_summary = Chemical treatment transcriptomic profiling\n"
                "!Series_supplementary_file = https://ftp.ncbi.nlm.nih.gov/geo/processed.tsv.gz\n"
                "!Sample_geo_accession = GSM1\n"
                "!Sample_organism_ch1 = Homo sapiens\n"
                "!Sample_characteristics_ch1 = A549 cells\n"
                "!Sample_treatment_protocol_ch1 = compound at 10 uM for 24 hours\n"
            ).encode()
        else:
            raise AssertionError(url)
        assert len(content) <= maximum_bytes
        return ScientificResponse(
            url=url,
            content=content,
            content_type=content_type,
            status_code=200,
            headers={"content-type": content_type, "content-length": str(len(content))},
            retrieved_at=time.time(),
        )


def _resource(
    provider: str,
    resource_id: str,
    kind: ProviderRecordKind,
    template: str,
) -> ProviderResourceTemplate:
    return ProviderResourceTemplate(
        resource_id=resource_id,
        logical_role=resource_id,
        record_kind=kind,
        locator_template=f"https://{HOST}/{provider}/{template}",
        item_kind=(ProviderItemKind.FILE if "{" not in template else ProviderItemKind.PAGE),
        accepted_mime_types=["application/json"],
        maximum_response_bytes=1024 * 1024,
    )


def _manifest(
    provider: str,
    mode: ProviderRetrievalMode,
    resources: list[ProviderResourceTemplate],
    *,
    page_size: int | None = None,
    identifier_batch_size: int | None = None,
    maximum_pages: int = 10,
) -> PreapprovalProviderReleaseManifest:
    return PreapprovalProviderReleaseManifest.create(
        release_id=f"fixture-{provider}-v1",
        provider=provider,
        adapter_id=provider,
        adapter_version="2.0.0",
        source_system=f"fixture {provider}",
        source_version="fixture-v1",
        source_locator=f"https://{HOST}/{provider}",
        reviewed_hosts=[HOST],
        retrieval_mode=mode,
        resources=resources,
        page_size=page_size,
        identifier_batch_size=identifier_batch_size,
        maximum_pages=maximum_pages,
        licence_and_provenance=[f"offline {provider} fixture"],
        release_checksum_or_citation="immutable fixture SHA-256",
        heavy_data_prohibited=["expression matrices", "dataset assembly"],
    )


def _registry() -> PreapprovalProviderReleaseRegistry:
    cursor = {
        "tox21": ProviderRecordKind.ASSAY,
        "pubchem-bioassay": ProviderRecordKind.ASSAY,
        "ncbi-geo": ProviderRecordKind.TRANSCRIPTOMIC,
        "ncbi-supporting-metadata": ProviderRecordKind.DATASET_LINK,
    }
    releases = [
        _manifest(
            "toxcast",
            ProviderRetrievalMode.MANIFEST_FILES,
            [
                _resource("toxcast", "release", ProviderRecordKind.RELEASE, "release"),
                _resource("toxcast", "compounds", ProviderRecordKind.COMPOUND, "compounds"),
                _resource("toxcast", "assays", ProviderRecordKind.ASSAY, "assays"),
                _resource("toxcast", "activity", ProviderRecordKind.ACTIVITY, "activity"),
            ],
        ),
        *[
            _manifest(
                provider,
                ProviderRetrievalMode.CURSOR_PAGES,
                [
                    _resource(
                        provider,
                        "catalogue_page",
                        kind,
                        "{page_start}",
                    )
                ],
                page_size=3,
            )
            for provider, kind in cursor.items()
        ],
        _manifest(
            "pubchem-compound",
            ProviderRetrievalMode.IDENTIFIER_BATCHES,
            [
                _resource(
                    "pubchem-compound",
                    "identity_batch",
                    ProviderRecordKind.COMPOUND,
                    "{identifier_batch}",
                )
            ],
            identifier_batch_size=2,
        ),
    ]
    return PreapprovalProviderReleaseRegistry(releases=releases)


def _executor_provider_registry(
    providers: tuple[str, ...] = ("tox21", "ncbi-geo", "pubchem-compound"),
) -> PreapprovalProviderReleaseRegistry:
    """Use tiny fixture locators under the exact production-planner release IDs."""

    release_ids = {
        "toxcast": "invitrodb-v4.3-2025-08",
        "tox21": "tox21-pubchem-reviewed-current",
        "pubchem-bioassay": "pubchem-bioassay-reviewed-current",
        "ncbi-geo": "ncbi-geo-reviewed-current",
        "pubchem-compound": "pubchem-compound-reviewed-current",
    }
    releases = []
    for manifest in _registry().releases:
        if manifest.provider not in providers:
            continue
        values = manifest.model_dump(mode="python", exclude={"manifest_fingerprint"})
        values["release_id"] = release_ids[manifest.provider]
        if manifest.provider in {"tox21", "pubchem-bioassay"}:
            for resource in values["resources"]:
                resource["locator_template"] += "?query={encoded_query}"
        releases.append(PreapprovalProviderReleaseManifest.create(**values))
    return PreapprovalProviderReleaseRegistry(releases=releases)


def _configure_semantics_v2_executor(
    database,
    store,
    service,
    tmp_path: Path,
    *,
    transport: FixtureTransport | None = None,
    providers: tuple[str, ...] = ("tox21", "ncbi-geo", "pubchem-compound"),
) -> tuple[SemanticsV2DiscoveryExecutor, FixtureTransport]:
    source = load_operational_capability_registry(REPO_ROOT)
    operational = OperationalCapabilityRegistry(
        providers=[source.by_provider(provider) for provider in providers]
    )
    registry_root = tmp_path / "registry" / "data"
    registry_root.mkdir(parents=True, exist_ok=True)
    (registry_root / "operational_provider_capabilities.json").write_text(
        operational.model_dump_json(indent=2), encoding="utf-8"
    )
    service.repo_root = tmp_path
    store.maximum_bytes = 20 * 1024 * 1024
    selected_transport = transport or FixtureTransport()
    layer = PreapprovalProviderLayer(
        PreapprovalMetadataProvider(
            _executor_provider_registry(providers),
            ProviderTaskExecutor(
                transport=selected_transport,
                cache=SourceResponseCache(database),
                artifacts=store,
            ),
        )
    )
    tools = extend_training_dataset_tool_registry(ToolRegistry(), preapproval_provider_layer=layer)
    executor = SemanticsV2DiscoveryExecutor(database, store, tools)
    service.semantics_v2_discovery_executor = executor
    return executor, selected_transport


def _authorized_executor_build(service, suffix: str):
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Example receptor binding agonism antagonism",
            endpoint_slug=f"semantics-v2-executor-{suffix}",
            biological_goal=(
                "Construct a public compound-level training dataset linking receptor "
                "activity modalities with transcriptomic responses."
            ),
            created_by="executor-test",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key=f"executor-create-{suffix}",
        )
    )
    waiting = service.start_build(
        created.id,
        expected_version=created.version,
        actor="executor-test",
        idempotency_key=f"executor-start-{suffix}",
    )
    approval = service.list_approvals(created.id, pending_only=True)[0]
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="executor-test",
            expected_version=waiting.version,
            idempotency_key=f"executor-approve-{suffix}",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    planned = service.continue_training_dataset_workflow(
        created.id,
        expected_version=approved.version,
        actor="executor-test",
        idempotency_key=f"executor-plan-{suffix}",
    )
    authorized = service.authorize_semantics_v2_discovery(
        created.id,
        expected_version=planned.version,
        actor="executor-test",
        idempotency_key=f"executor-authorize-{suffix}",
    )
    assert authorized.current_stage is WorkflowState.DISCOVERING_SOURCE_CANDIDATES
    return authorized


def _workflow_id(service) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Preapproval provider fixture",
            endpoint_slug="preapproval-provider-fixture",
            biological_goal="Verify complete metadata-only provider execution.",
            created_by="preapproval-test",
            idempotency_key="preapproval-provider-fixture",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
        )
    ).id


def _record(provider: str, role: EvidenceRole, ordinal: int) -> DiscoveryExecutionRecord:
    return DiscoveryExecutionRecord(
        task_id=f"task-{provider}-{ordinal}",
        provider=provider,
        evidence_role=role,
        modality=("binding" if role is EvidenceRole.ACTIVITY else None),
        status=DiscoveryTaskStatus.PENDING,
    )


def _invocation(workflow_id: str, provider: str) -> ToolInvocation:
    return ToolInvocation(
        tool_name=PROVIDER_TOOL_NAMES[provider],
        arguments={},
        workflow_id=workflow_id,
        workflow_stage=WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
        idempotency_key=f"preapproval:{provider}",
    )


def _identifier_artifact(store, workflow_id: str) -> ArtifactReference:
    values = ["CID:101", "CID:102", "CID:103", "CID:104", "CID:105"]
    descriptor = store.put_bytes(
        workflow_id=workflow_id,
        content=("\n".join(values) + "\n").encode(),
        mime_type="text/plain",
        artifact_type="complete_upstream_compound_identifier_index",
        logical_name="preapproval-identifiers.ndjson",
        producer="preapproval-offline-fixture",
        idempotency_key="preapproval-identifiers",
    )
    return ArtifactReference(
        artifact_id=descriptor.id,
        sha256=descriptor.sha256,
        artifact_type=descriptor.artifact_type,
    )


def _open_sqlite(store, artifact: ArtifactReference) -> sqlite3.Connection:
    _, path = store.verified_path(artifact.artifact_id)
    return sqlite3.connect(path)


def test_complete_multi_provider_execution_and_cache_replay(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 20 * 1024 * 1024
    workflow_id = _workflow_id(service)
    transport = FixtureTransport()
    layer = PreapprovalProviderLayer(
        PreapprovalMetadataProvider(
            _registry(),
            ProviderTaskExecutor(
                transport=transport,
                cache=SourceResponseCache(database),
                artifacts=store,
            ),
        )
    )
    roles = {
        "toxcast": EvidenceRole.ACTIVITY,
        "tox21": EvidenceRole.ACTIVITY,
        "pubchem-bioassay": EvidenceRole.ACTIVITY,
        "pubchem-compound": EvidenceRole.IDENTITY,
        "ncbi-geo": EvidenceRole.TRANSCRIPTOMIC,
        "ncbi-supporting-metadata": EvidenceRole.SUPPORTING_METADATA,
    }
    identity_artifact = _identifier_artifact(store, workflow_id)
    outputs = {}
    for ordinal, (provider, role) in enumerate(roles.items(), start=1):
        outputs[provider] = layer.execute(
            provider,
            ProviderMetadataExecutionInput(
                ledger_record=_record(provider, role, ordinal),
                release_id=f"fixture-{provider}-v1",
                query=ProviderMetadataQuery(
                    biological_target="example receptor",
                    target_identifiers=["EXAMPLE:1"],
                    target_synonyms=["example target"],
                    modality="binding" if role is EvidenceRole.ACTIVITY else None,
                    publication_identifiers=(
                        ["PMID:12345", "DOI:10.1000/example"]
                        if role is EvidenceRole.SUPPORTING_METADATA
                        else []
                    ),
                ),
                upstream_identifier_artifact=(
                    identity_artifact if provider == "pubchem-compound" else None
                ),
                upstream_identifier_count=(5 if provider == "pubchem-compound" else 0),
            ),
            _invocation(workflow_id, provider),
        )
    assert len(transport.calls) == 14, "\n".join(transport.calls)
    assert all(hosts == frozenset({HOST}) for hosts in transport.approved_request_hosts)
    assert all(item.completion_proof.completed for item in outputs.values()), {
        key: value.completion_proof.model_dump(mode="json") for key, value in outputs.items()
    }
    assert all(
        item.ledger_record.status is DiscoveryTaskStatus.COMPLETED for item in outputs.values()
    )
    assert all(item.dataset_manifest is not None for item in outputs.values())
    assert all(not item.dataset_manifest.expression_values_retrieved for item in outputs.values())
    assert all(
        not item.dataset_manifest.final_source_selection_performed for item in outputs.values()
    )
    assert outputs["toxcast"].dataset_manifest.counts.activity_records == 3
    assert set(outputs["toxcast"].dataset_manifest.modalities) == {"agonism", "binding"}
    assert outputs["tox21"].dataset_manifest.counts.assay_relationships == 2
    assert outputs["pubchem-bioassay"].dataset_manifest.counts.activity_records == 2
    assert outputs["pubchem-compound"].completion_proof.processed_identifier_count == 5
    assert outputs["pubchem-compound"].dataset_manifest.counts.exact_identity_mappings == 2
    assert outputs["pubchem-compound"].dataset_manifest.counts.ambiguous_identity_mappings == 2
    assert outputs["pubchem-compound"].dataset_manifest.counts.missing_identity_mappings == 1
    assert outputs["ncbi-geo"].dataset_manifest.counts.transcriptomic_records == 4
    assert (
        outputs["ncbi-supporting-metadata"].dataset_manifest.counts.publication_dataset_links == 2
    )

    with _open_sqlite(store, outputs["toxcast"].dataset_manifest.sqlite_artifact) as connection:
        assert connection.execute(
            "SELECT activity_call FROM activity_records WHERE activity_value='1.73'"
        ).fetchone() == (None,)
    with _open_sqlite(store, outputs["ncbi-geo"].dataset_manifest.sqlite_artifact) as connection:
        assert {
            row[0] for row in connection.execute("SELECT outcome FROM transcriptomic_records")
        } == {
            "verified_compound_perturbation",
            "unrelated_name_match",
            "biological_context_incomplete",
            "processed_matrix_missing",
        }

    calls_before = list(transport.calls)
    replay_fingerprints = {}
    for ordinal, (provider, role) in enumerate(roles.items(), start=1):
        replay = layer.execute(
            provider,
            ProviderMetadataExecutionInput(
                ledger_record=_record(provider, role, ordinal),
                release_id=f"fixture-{provider}-v1",
                query=ProviderMetadataQuery(
                    biological_target="example receptor",
                    target_identifiers=["EXAMPLE:1"],
                    target_synonyms=["example target"],
                    modality="binding" if role is EvidenceRole.ACTIVITY else None,
                    publication_identifiers=(
                        ["PMID:12345", "DOI:10.1000/example"]
                        if role is EvidenceRole.SUPPORTING_METADATA
                        else []
                    ),
                ),
                upstream_identifier_artifact=(
                    identity_artifact if provider == "pubchem-compound" else None
                ),
                upstream_identifier_count=(5 if provider == "pubchem-compound" else 0),
            ),
            _invocation(workflow_id, provider),
        )
        assert replay.cache_only_replay
        assert not replay.normalization_ran
        replay_fingerprints[provider] = replay.dataset_manifest.bundle_fingerprint
    assert transport.calls == calls_before
    assert replay_fingerprints == {
        provider: output.dataset_manifest.bundle_fingerprint for provider, output in outputs.items()
    }


def test_manifest_allowlist_is_endpoint_scoped_and_local_policy_failure_is_not_transport(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow_id(service)
    transport = LocalPolicyFailureTransport()
    provider = PreapprovalMetadataProvider(
        _registry(),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )

    output = provider.execute(
        "toxcast",
        ProviderMetadataExecutionInput(
            ledger_record=_record("toxcast", EvidenceRole.ACTIVITY, 1),
            release_id="fixture-toxcast-v1",
            query=ProviderMetadataQuery(
                biological_target="example receptor",
                modality="binding",
            ),
        ),
        _invocation(workflow_id, "toxcast"),
    )

    assert len(transport.calls) == 4
    assert all(hosts == frozenset({HOST}) for hosts in transport.approved_request_hosts)
    assert not output.completion_proof.completed
    assert output.ledger_record.scientific_source_request_count == 0
    assert output.ledger_record.transport_attempt_count == 0
    assert all(
        not attempt.request_left_process
        for execution in output.page_or_file_execution_preview
        for result in execution.page_or_file_results
        for attempt in result.transport_attempts
    )


def test_registry_planning_accepts_live_validated_preapproval_providers() -> None:
    registry = load_operational_capability_registry(REPO_ROOT)
    artifact = ArtifactReference(
        artifact_id="art-approved-specification",
        sha256="a" * 64,
        artifact_type="training_dataset_specification",
    )
    plan = build_discovery_plan(
        workflow_id="build-provider-preapproval",
        endpoint_identifier="example-receptor",
        approved_specification_artifact=artifact,
        discovery_round=0,
        biological_target="example receptor",
        target_synonyms=["example target"],
        requested_modalities=["binding", "agonism", "antagonism"],
        registry=registry,
        budgets=DiscoveryBudgets(
            maximum_provider_invocations=32,
            maximum_tool_calls=64,
            maximum_scientific_source_requests=2000,
            maximum_input_tokens=0,
            maximum_output_tokens=0,
            maximum_estimated_cost_usd=0,
            timeout_seconds=1200,
        ),
    )
    ledger, findings = initial_execution_ledger(plan, registry)
    assert findings.findings == []
    assert all(
        record.status is not DiscoveryTaskStatus.BLOCKED_PROVIDER_CAPABILITY_MISSING
        for record in ledger.records
    )
    operations = {item.provider: item.operation for item in plan.provider_specific_query_tasks}
    assert operations["toxcast"] == "retrieve_toxcast_preapproval_metadata"
    assert operations["tox21"] == "retrieve_tox21_preapproval_metadata"
    assert operations["ncbi-geo"] == "retrieve_geo_preapproval_metadata"
    assert operations["pubchem-compound"] == "resolve_pubchem_compound_full_set"
    assert all(
        item.pagination_policy.maximum_candidates is None
        for item in plan.provider_specific_query_tasks
    )
    assert all(
        not item.pagination_policy.top_n_is_completion
        for item in plan.provider_specific_query_tasks
    )
    assert all(
        item.query_parameters["source_hints"] == [] for item in plan.provider_specific_query_tasks
    )
    records = {item.task_id: item for item in ledger.records}
    for task in plan.provider_specific_query_tasks:
        if task.provider == "lincs-l1000":
            continue
        compiled = compile_provider_metadata_input(task, records[task.task_id])
        assert compiled.ledger_record.task_id == task.task_id
        assert compiled.release_id == task.query_parameters["release_id"]
        assert compiled.query.source_hints == []


def test_non_receptor_plan_uses_different_provider_and_modality_configuration() -> None:
    source = load_operational_capability_registry(REPO_ROOT)
    providers = [
        source.by_provider(provider) for provider in FIXTURE["non_receptor_endpoint"]["providers"]
    ]
    registry = OperationalCapabilityRegistry(providers=providers)
    plan = build_discovery_plan(
        workflow_id="build-non-receptor-preapproval",
        endpoint_identifier="oxidative-stress-response",
        approved_specification_artifact=ArtifactReference(
            artifact_id="art-non-receptor-spec",
            sha256="b" * 64,
            artifact_type="training_dataset_specification",
        ),
        discovery_round=0,
        biological_target=FIXTURE["non_receptor_endpoint"]["endpoint"],
        target_synonyms=["cellular oxidative stress"],
        requested_modalities=FIXTURE["non_receptor_endpoint"]["modalities"],
        registry=registry,
        budgets=DiscoveryBudgets(
            maximum_provider_invocations=8,
            maximum_tool_calls=16,
            maximum_scientific_source_requests=100,
            maximum_input_tokens=0,
            maximum_output_tokens=0,
            maximum_estimated_cost_usd=0,
            timeout_seconds=600,
        ),
    )
    assert plan.requested_modalities == ["activation"]
    assert set(plan.applicable_registered_providers) == {
        "toxcast",
        "ncbi-geo",
        "pubchem-compound",
    }
    assert not any("receptor" in item.task_id for item in plan.provider_specific_query_tasks)


def test_pubchem_identity_processes_every_upstream_compound_without_sampling(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 20 * 1024 * 1024
    workflow_id = _workflow_id(service)
    identifiers = [f"CID:{index}" for index in range(1, 28)]
    descriptor = store.put_bytes(
        workflow_id=workflow_id,
        content=("\n".join(identifiers) + "\n").encode(),
        mime_type="text/plain",
        artifact_type="complete_upstream_compound_identifier_index",
        logical_name="preapproval-arbitrary-identifiers.ndjson",
        producer="preapproval-offline-fixture",
        idempotency_key="preapproval-arbitrary-identifiers",
    )
    transport = ArbitraryCompoundTransport()
    provider = PreapprovalMetadataProvider(
        _registry(),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    output = provider.execute(
        "pubchem-compound",
        ProviderMetadataExecutionInput(
            ledger_record=_record("pubchem-compound", EvidenceRole.IDENTITY, 1),
            release_id="fixture-pubchem-compound-v1",
            query=ProviderMetadataQuery(biological_target="non-receptor endpoint"),
            upstream_identifier_artifact=ArtifactReference(
                artifact_id=descriptor.id,
                sha256=descriptor.sha256,
                artifact_type=descriptor.artifact_type,
            ),
            upstream_identifier_count=len(identifiers),
        ),
        _invocation(workflow_id, "pubchem-compound"),
    )
    assert len(transport.calls) == 14
    assert output.completion_proof.all_upstream_compounds_processed
    assert output.completion_proof.processed_identifier_count == 27
    assert output.dataset_manifest.counts.compound_mappings == 27
    assert output.candidates.exact_count == 27
    assert len(output.candidates.preview) == 20
    assert not output.candidates.preview_is_complete


def test_production_pubchem_locator_accepts_only_canonical_cid_identifiers() -> None:
    manifest = load_preapproval_provider_registry(REPO_ROOT).release(
        "pubchem-compound", "pubchem-compound-reviewed-current"
    )
    template = manifest.resources[0]
    locator = _render_locator(
        template.locator_template,
        query=ProviderMetadataQuery(),
        page=1,
        page_size=100,
        cursor=None,
        identifiers=["CID:2244", "CID:2519"],
        identifier_path_kind=template.identifier_path_kind,
    )
    assert "/cid/2244,2519/property/" in locator
    assert "CID%3A" not in locator
    with pytest.raises(ValueError, match="canonical CID"):
        _render_locator(
            template.locator_template,
            query=ProviderMetadataQuery(),
            page=1,
            page_size=100,
            cursor=None,
            identifiers=["AID:2244"],
            identifier_path_kind=template.identifier_path_kind,
        )


def test_typed_tools_are_distinct_and_preapproval_only(workflow_runtime) -> None:
    database, store, _providers, _harness, _service = workflow_runtime
    layer = PreapprovalProviderLayer(
        PreapprovalMetadataProvider(
            _registry(),
            ProviderTaskExecutor(
                transport=FixtureTransport(),
                cache=SourceResponseCache(database),
                artifacts=store,
            ),
        )
    )
    registry = extend_training_dataset_tool_registry(
        ToolRegistry(), preapproval_provider_layer=layer
    )
    inventory = {item.name: item for item in registry.definitions()}
    assert set(PROVIDER_TOOL_NAMES.values()) <= set(inventory)
    assert len(set(PROVIDER_TOOL_NAMES.values())) == 6
    assert all(
        inventory[name].access_phase.value == "pre_approval_metadata"
        for name in PROVIDER_TOOL_NAMES.values()
    )
    assert inventory["slice_approved_lincs_level5_expression"].access_phase.value == (
        "post_approval_extraction"
    )


def test_semantics_v2_executor_dispatches_every_registered_provider_without_substitution() -> None:
    assert PROVIDER_OPERATIONS == {
        **PROVIDER_TOOL_NAMES,
        "lincs-l1000": "retrieve_lincs_metadata_release",
    }


def test_semantics_v2_executor_runs_typed_providers_hydrates_and_replays(
    workflow_runtime, tmp_path
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    _executor, transport = _configure_semantics_v2_executor(database, store, service, tmp_path)
    authorized = _authorized_executor_build(service, "success")
    workflow_before = service.training_dataset_workflow(authorized.id)
    assert len(workflow_before["discovery_plan"]["provider_specific_query_tasks"]) == 5
    assert {
        item["modality"]
        for item in workflow_before["discovery_plan"]["provider_specific_query_tasks"]
        if item["provider"] == "tox21"
    } == {"binding", "agonism", "antagonism"}

    discovered = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-success-discovery",
    )
    assert discovered.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert discovered.discovery_progress is not None
    assert discovered.discovery_progress["ledger_task_total"] == 5
    assert discovered.discovery_progress["candidate_count"] > 0
    assert discovered.discovery_progress["failed_task_count"] == 0
    calls_after_discovery = list(transport.calls)
    candidate_descriptor = store.find_by_logical_name(
        authorized.id, "source-candidates-round-0.json"
    )
    assert candidate_descriptor is not None

    replayed_discovery = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-success-discovery",
    )
    assert replayed_discovery.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert transport.calls == calls_after_discovery
    assert (
        store.find_by_logical_name(authorized.id, "source-candidates-round-0.json").sha256
        == candidate_descriptor.sha256
    )

    hydrated = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=discovered.version,
        actor="executor-test",
        idempotency_key="executor-success-hydration",
    )
    assert hydrated.current_stage is WorkflowState.COMPUTING_COMBINATION_COVERAGE
    assert hydrated.discovery_progress is not None
    assert (
        hydrated.discovery_progress["hydrated_source_count"]
        == (hydrated.discovery_progress["candidate_count"])
    )
    assert transport.calls == calls_after_discovery
    hydrated_descriptor = store.find_by_logical_name(authorized.id, "hydrated-sources-round-0.json")
    assert hydrated_descriptor is not None

    replayed_hydration = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=discovered.version,
        actor="executor-test",
        idempotency_key="executor-success-hydration",
    )
    assert replayed_hydration.current_stage is WorkflowState.COMPUTING_COMBINATION_COVERAGE
    assert transport.calls == calls_after_discovery
    assert (
        store.find_by_logical_name(authorized.id, "hydrated-sources-round-0.json").sha256
        == hydrated_descriptor.sha256
    )
    final_workflow = service.training_dataset_workflow(authorized.id)
    assert final_workflow["source_candidates"] is not None
    assert final_workflow["hydrated_sources"] is not None
    assert final_workflow["combination_coverage"] is None
    assert final_workflow["strategy_proposals"] is None
    assert final_workflow["assembly_recipe"] is None
    assert not any(
        item.artifact_type
        in {"combination_coverage", "strategy_proposals", "assembly_recipe", "expression_matrix"}
        for item in store.list_artifacts(authorized.id)
    )


class OneProviderFailureTransport(FixtureTransport):
    def get(self, url: str, **kwargs) -> ScientificResponse:
        if "/ncbi-geo/" in url:
            self.calls.append(url)
            raise SourcePolicyError("Offline fixture GEO transport failure.")
        return super().get(url, **kwargs)


def test_semantics_v2_executor_preserves_partial_failure_and_continues(
    workflow_runtime, tmp_path
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        transport=OneProviderFailureTransport(),
    )
    authorized = _authorized_executor_build(service, "partial")
    discovered = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-partial-discovery",
    )
    assert discovered.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    progress = executor.progress(authorized.id, discovered.current_stage, discovered.version)
    assert progress["failed_task_count"] == 1
    assert progress["candidate_count"] > 0
    assert progress["safe_failure_summary"][0]["error_classification"]
    workflow = service.training_dataset_workflow(authorized.id)
    candidates = workflow["source_candidates"]
    assert not candidates["complete_without_failures"]
    assert len(candidates["failed_task_ids"]) == 1
    failed_id = candidates["failed_task_ids"][0]
    record = next(
        item
        for item in workflow["discovery_execution_ledger"]["records"]
        if item["task_id"] == failed_id
    )
    assert record["status"] == "failed"
    assert record["completion_reason"] == "Offline fixture GEO transport failure."
    assert record["source_response_artifacts"]
    assert any("/ncbi-geo/" in call for call in transport.calls)
    assert any("/tox21/" in call for call in transport.calls)

    hydrated = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=discovered.version,
        actor="executor-test",
        idempotency_key="executor-partial-hydration",
    )
    assert hydrated.current_stage is WorkflowState.COMPUTING_COMBINATION_COVERAGE
    hydrated_document = service.training_dataset_workflow(authorized.id)["hydrated_sources"]
    assert not hydrated_document["complete_without_failures"]
    assert hydrated_document["failed_task_ids"] == [failed_id]


def test_semantics_v2_executor_resumes_only_pending_tasks_after_interruption(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, transport = _configure_semantics_v2_executor(database, store, service, tmp_path)
    authorized = _authorized_executor_build(service, "resume")
    original_terminalize = executor._terminalize
    terminal_count = 0

    def interrupt_after_first(workflow_id, materialization):
        nonlocal terminal_count
        reference = original_terminalize(workflow_id, materialization)
        terminal_count += 1
        if terminal_count == 1:
            raise RuntimeError("offline interruption after durable task checkpoint")
        return reference

    monkeypatch.setattr(executor, "_terminalize", interrupt_after_first)
    with pytest.raises(RuntimeError, match="offline interruption"):
        service.continue_training_dataset_workflow(
            authorized.id,
            expected_version=authorized.version,
            actor="executor-test",
            idempotency_key="executor-resume-interrupted",
        )
    calls_after_first = list(transport.calls)
    interrupted = service.training_dataset_workflow(authorized.id)
    terminal_successes = {
        "completed",
        "completed_no_candidates",
    }
    assert (
        sum(
            item["status"] in terminal_successes
            for item in interrupted["discovery_execution_ledger"]["records"]
        )
        == 1
    )
    monkeypatch.setattr(executor, "_terminalize", original_terminalize)

    resumed = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-resume-complete",
    )
    assert resumed.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert transport.calls[: len(calls_after_first)] == calls_after_first
    assert len(transport.calls) > len(calls_after_first)
    task_results = [
        item
        for item in store.list_artifacts(authorized.id)
        if item.artifact_type == "semantics_v2_discovery_task_result"
    ]
    assert len(task_results) == 5
    assert len({item.sha256 for item in task_results}) == 5


def test_semantics_v2_executor_recovers_a_stale_running_claim(workflow_runtime, tmp_path) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, _transport = _configure_semantics_v2_executor(database, store, service, tmp_path)
    authorized = _authorized_executor_build(service, "recovery")
    _plan, ledger = executor._load(authorized.id)
    pending = next(item for item in ledger.records if item.status is DiscoveryTaskStatus.PENDING)
    executor._checkpoint_ledger_record(
        authorized.id,
        task_id=pending.task_id,
        expected_statuses={DiscoveryTaskStatus.PENDING},
        record=pending.model_copy(update={"status": DiscoveryTaskStatus.RUNNING}),
        actor="offline-interruption-fixture",
    )

    assert executor.recover_interrupted() == 1
    _plan, recovered = executor._load(authorized.id)
    recovered_record = next(item for item in recovered.records if item.task_id == pending.task_id)
    assert recovered_record.status is DiscoveryTaskStatus.PENDING
    assert recovered_record.retry_provenance == ["resumed_from_interrupted_durable_claim"]
    recovery_artifact = store.find_by_logical_name(
        authorized.id, "discovery-ledger-round-0-startup-recovery.json"
    )
    assert recovery_artifact is not None

    resumed = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-recovery-complete",
    )
    assert resumed.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES


def test_post_provider_materialization_failure_terminalizes_and_tox21_continues(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, _transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        transport=ModalityAwareFixtureTransport(),
    )
    authorized = _authorized_executor_build(service, "post-provider-failure")
    original_provider_rows = executor._provider_rows
    failed_once = False

    def fail_first_materialization(workflow_id, task, output):
        nonlocal failed_once
        if task.provider == "tox21" and task.modality == "binding" and not failed_once:
            failed_once = True
            raise WorkflowConflict("Offline candidate persistence failure.")
        return original_provider_rows(workflow_id, task, output)

    monkeypatch.setattr(executor, "_provider_rows", fail_first_materialization)
    discovered = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-post-provider-failure",
    )

    assert discovered.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    workflow = service.training_dataset_workflow(authorized.id)
    records = workflow["discovery_execution_ledger"]["records"]
    binding = next(
        item for item in records if item["provider"] == "tox21" and item["modality"] == "binding"
    )
    assert binding["status"] == "completed_with_partial_results"
    assert binding["error_classification"] == "provider_result_materialization_failed"
    assert binding["source_response_artifacts"]
    assert not any(item["status"] == "running" for item in records)
    assert {
        item["modality"]
        for item in records
        if item["provider"] == "tox21" and item["status"] == "completed"
    } == {"agonism", "antagonism"}

    candidates = workflow["source_candidates"]
    assert candidates["partial_task_ids"] == [binding["task_id"]]
    assert candidates["pending_task_ids"] == []
    tox21_candidates = [item for item in candidates["candidates"] if item["provider"] == "tox21"]
    assert tox21_candidates
    assert all(item["source_identifier"].startswith("AID:") for item in tox21_candidates)
    assert {item["modality"] for item in tox21_candidates} == {"agonism", "antagonism"}
    assert all(item["provider_dataset_artifacts"] for item in tox21_candidates)

    hydrated = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=discovered.version,
        actor="executor-test",
        idempotency_key="executor-post-provider-hydration",
    )
    assert hydrated.current_stage is WorkflowState.COMPUTING_COMBINATION_COVERAGE
    hydrated_sources = service.training_dataset_workflow(authorized.id)["hydrated_sources"]
    assert hydrated_sources["partial_task_ids"] == [binding["task_id"]]
    assert all(
        item["evidence_artifacts"]
        for item in hydrated_sources["sources"]
        if item["provider"] == "tox21"
    )


def test_stale_running_task_resumes_from_provider_output_without_provider_reexecution(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, _transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        transport=ModalityAwareFixtureTransport(),
    )
    authorized = _authorized_executor_build(service, "provider-evidence-resume")
    original_persist = executor._persist_provider_output
    interrupted_task_id: str | None = None

    def persist_then_interrupt(workflow_id, plan, task, output):
        nonlocal interrupted_task_id
        original_persist(workflow_id, plan, task, output)
        interrupted_task_id = task.task_id
        raise KeyboardInterrupt("offline process interruption after provider checkpoint")

    monkeypatch.setattr(executor, "_persist_provider_output", persist_then_interrupt)
    with pytest.raises(KeyboardInterrupt, match="offline process interruption"):
        service.continue_training_dataset_workflow(
            authorized.id,
            expected_version=authorized.version,
            actor="executor-test",
            idempotency_key="executor-provider-evidence-interrupted",
        )
    assert interrupted_task_id is not None
    interrupted = service.training_dataset_workflow(authorized.id)
    record = next(
        item
        for item in interrupted["discovery_execution_ledger"]["records"]
        if item["task_id"] == interrupted_task_id
    )
    assert record["status"] == "running"
    assert (
        store.find_by_logical_name(
            authorized.id,
            f"semantics-v2-provider-output-round-0-{interrupted_task_id}.json",
        )
        is not None
    )

    monkeypatch.setattr(executor, "_persist_provider_output", original_persist)
    assert executor.recover_interrupted() == 1
    original_invoke = executor._invoke_task
    invoked_task_ids: list[str] = []

    def track_invocation(workflow_id, plan, task, running):
        invoked_task_ids.append(task.task_id)
        return original_invoke(workflow_id, plan, task, running)

    monkeypatch.setattr(executor, "_invoke_task", track_invocation)
    resumed = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-provider-evidence-resumed",
    )
    assert resumed.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert interrupted_task_id not in invoked_task_ids
    resumed_workflow = service.training_dataset_workflow(authorized.id)
    resumed_record = next(
        item
        for item in resumed_workflow["discovery_execution_ledger"]["records"]
        if item["task_id"] == interrupted_task_id
    )
    assert resumed_record["status"] == "completed"
    assert resumed_record["retry_provenance"] == ["resumed_from_interrupted_durable_claim"]

    candidate_descriptor = store.find_by_logical_name(
        authorized.id, "source-candidates-round-0.json"
    )
    assert candidate_descriptor is not None
    task_result_count = len(
        [
            item
            for item in store.list_artifacts(authorized.id)
            if item.artifact_type == "semantics_v2_discovery_task_result"
        ]
    )
    repeated = executor.execute_discovery(authorized.id)
    assert repeated.candidate_set is not None
    assert (
        store.find_by_logical_name(authorized.id, "source-candidates-round-0.json").sha256
        == candidate_descriptor.sha256
    )
    assert (
        len(
            [
                item
                for item in store.list_artifacts(authorized.id)
                if item.artifact_type == "semantics_v2_discovery_task_result"
            ]
        )
        == task_result_count
    )


def test_stale_toxcast_task_recovers_legacy_provider_artifacts_without_retrieval(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        providers=("toxcast",),
    )
    authorized = _authorized_executor_build(service, "legacy-toxcast-evidence-resume")
    original_persist = executor._persist_provider_output
    interrupted_task_id: str | None = None

    def interrupt_before_checkpoint(workflow_id, plan, task, output):
        del workflow_id, plan, output
        nonlocal interrupted_task_id
        interrupted_task_id = task.task_id
        raise KeyboardInterrupt("offline interruption before executor provider checkpoint")

    monkeypatch.setattr(executor, "_persist_provider_output", interrupt_before_checkpoint)
    with pytest.raises(KeyboardInterrupt, match="before executor provider checkpoint"):
        service.continue_training_dataset_workflow(
            authorized.id,
            expected_version=authorized.version,
            actor="executor-test",
            idempotency_key="executor-legacy-toxcast-interrupted",
        )
    assert interrupted_task_id is not None
    assert (
        store.find_by_logical_name(
            authorized.id,
            f"semantics-v2-provider-output-round-0-{interrupted_task_id}.json",
        )
        is None
    )
    artifact_types = {item.artifact_type for item in store.list_artifacts(authorized.id)}
    assert "preapproval_provider_execution_manifest" in artifact_types
    assert "preapproval_provider_normalization_manifest" in artifact_types
    external_calls_before_recovery = list(transport.calls)

    monkeypatch.setattr(executor, "_persist_provider_output", original_persist)
    assert executor.recover_interrupted() == 1
    original_invoke = executor._invoke_task
    invoked_task_ids: list[str] = []

    def track_invocation(workflow_id, plan, task, running):
        invoked_task_ids.append(task.task_id)
        return original_invoke(workflow_id, plan, task, running)

    monkeypatch.setattr(executor, "_invoke_task", track_invocation)
    resumed = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-legacy-toxcast-resumed",
    )
    assert resumed.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert interrupted_task_id not in invoked_task_ids
    assert transport.calls[: len(external_calls_before_recovery)] == external_calls_before_recovery
    resumed_workflow = service.training_dataset_workflow(authorized.id)
    resumed_record = next(
        item
        for item in resumed_workflow["discovery_execution_ledger"]["records"]
        if item["task_id"] == interrupted_task_id
    )
    assert resumed_record["status"] == "completed"
    assert resumed_record["retry_provenance"] == ["resumed_from_interrupted_durable_claim"]
    assert (
        store.find_by_logical_name(
            authorized.id,
            f"semantics-v2-provider-output-round-0-{interrupted_task_id}.json",
        )
        is not None
    )


def test_completed_legacy_pubchem_result_is_compacted_without_provider_reexecution(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        transport=ModalityAwareFixtureTransport(),
        providers=("pubchem-bioassay", "ncbi-geo", "pubchem-compound"),
    )
    authorized = _authorized_executor_build(service, "legacy-pubchem-compaction")
    discovered = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-legacy-pubchem-discovered",
    )
    assert discovered.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    plan, ledger = executor._load(authorized.id)
    record = next(
        item
        for item in ledger.records
        if item.provider == "pubchem-bioassay" and item.status is DiscoveryTaskStatus.COMPLETED
    )
    original_load = executor._load_task_result
    loaded = original_load(authorized.id, plan.discovery_round, record.task_id)
    assert loaded is not None
    current, reference = loaded
    legacy = current.model_copy(
        update={
            "candidates": [
                item.model_copy(
                    update={
                        "contributing_task_ids": [],
                        "contributing_modalities": [],
                        "release_id": None,
                        "source_version": None,
                        "provider_dataset_artifacts": [],
                        "coverage_summary": {},
                        "relationship_summary": {},
                    }
                )
                for item in current.candidates
            ],
            "hydrated_sources": [
                item.model_copy(
                    update={
                        "contributing_task_ids": [],
                        "contributing_modalities": [],
                        "release_id": None,
                        "coverage_summary": {},
                        "relationship_summary": {},
                    }
                )
                for item in current.hydrated_sources
            ],
        }
    )

    def load_legacy_result(workflow_id, round_number, task_id):
        if task_id == record.task_id:
            return legacy, reference
        return original_load(workflow_id, round_number, task_id)

    monkeypatch.setattr(executor, "_load_task_result", load_legacy_result)
    external_calls_before = list(transport.calls)
    materializations = executor._materializations(authorized.id, plan, ledger)
    assert transport.calls == external_calls_before
    compacted = next(item for item in materializations if item.task_id == record.task_id)
    assert compacted.candidates
    assert all(item.release_id is not None for item in compacted.candidates)
    assert all(item.contributing_task_ids == [record.task_id] for item in compacted.candidates)
    assert all(item.provider_dataset_artifacts for item in compacted.candidates)
    assert all(
        item.verified_metadata["scientific_source_unit"] == "assay_endpoint"
        for item in compacted.hydrated_sources
    )


def test_pubchem_timeout_does_not_block_other_pubchem_or_tox21_tasks(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, _transport = _configure_semantics_v2_executor(
        database,
        store,
        service,
        tmp_path,
        transport=ModalityAwareFixtureTransport(),
        providers=("pubchem-bioassay", "tox21", "ncbi-geo", "pubchem-compound"),
    )
    authorized = _authorized_executor_build(service, "pubchem-timeout-continuation")
    original_invoke = executor._invoke_task

    def timeout_binding(workflow_id, plan, task, running):
        if task.provider == "pubchem-bioassay" and task.modality == "binding":
            evidence = store.put_json(
                workflow_id=workflow_id,
                value={"partial_raw_evidence": True},
                artifact_type="offline_partial_provider_evidence",
                logical_name="offline-pubchem-binding-timeout-evidence.json",
                producer="offline-test",
                idempotency_key="offline-pubchem-binding-timeout-evidence",
            )
            reference = ArtifactReference(
                artifact_id=evidence.id,
                sha256=evidence.sha256,
                artifact_type=evidence.artifact_type,
            )
            terminal = running.model_copy(
                update={
                    "status": DiscoveryTaskStatus.FAILED,
                    "completion_reason": "Tool exceeded its configured timeout.",
                    "error_classification": "tool_timeout",
                    "source_response_artifacts": [reference],
                    "logical_tool_call_count": 1,
                }
            )
            return DiscoveryTaskMaterialization(
                workflow_id=workflow_id,
                discovery_round=plan.discovery_round,
                plan_fingerprint=plan.plan_fingerprint,
                task_id=task.task_id,
                ledger_record=terminal,
                provider_dataset_artifacts=[reference],
                safe_failure_message=terminal.completion_reason,
            )
        return original_invoke(workflow_id, plan, task, running)

    monkeypatch.setattr(executor, "_invoke_task", timeout_binding)
    discovered = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-pubchem-timeout-continuation",
    )
    assert discovered.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    workflow = service.training_dataset_workflow(authorized.id)
    records = workflow["discovery_execution_ledger"]["records"]
    binding = next(
        item
        for item in records
        if item["provider"] == "pubchem-bioassay" and item["modality"] == "binding"
    )
    assert binding["status"] == "failed"
    assert binding["error_classification"] == "tool_timeout"
    assert binding["source_response_artifacts"]
    assert {
        item["modality"]
        for item in records
        if item["provider"] == "pubchem-bioassay" and item["status"] == "completed"
    } == {"agonism", "antagonism"}
    assert {
        item["modality"]
        for item in records
        if item["provider"] == "tox21" and item["status"] == "completed"
    } == {"binding", "agonism", "antagonism"}
    assert workflow["source_candidates"]["failed_task_ids"] == [binding["task_id"]]
    assert workflow["source_candidates"]["pending_task_ids"] == []
    tox21_candidates = [
        item for item in workflow["source_candidates"]["candidates"] if item["provider"] == "tox21"
    ]
    assert {item["modality"] for item in tox21_candidates} == {
        "binding",
        "agonism",
        "antagonism",
    }
    assert all(item["source_identifier"].startswith("AID:") for item in tox21_candidates)
    assert all(
        any(
            reference["artifact_type"].endswith("_preapproval_metadata_sqlite")
            for reference in item["provider_dataset_artifacts"]
        )
        for item in tox21_candidates
    )
    relationship_types = {
        relationship_type
        for item in tox21_candidates
        for summary in item["relationship_summary"]["by_task"].values()
        for relationship_type in summary["relationship_types"]
    }
    assert {"summary_component", "viability_counterscreen"} <= relationship_types

    hydrated = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=discovered.version,
        actor="executor-test",
        idempotency_key="executor-pubchem-timeout-hydration",
    )
    assert hydrated.current_stage is WorkflowState.COMPUTING_COMBINATION_COVERAGE
    final_workflow = service.training_dataset_workflow(authorized.id)
    tox21_hydrated = [
        item
        for item in final_workflow["hydrated_sources"]["sources"]
        if item["provider"] == "tox21"
    ]
    assert {item["modality"] for item in tox21_hydrated} == {
        "binding",
        "agonism",
        "antagonism",
    }
    assert all(item["evidence_artifacts"] for item in tox21_hydrated)
    assert final_workflow["hydrated_sources"]["failed_task_ids"] == [binding["task_id"]]
    assert final_workflow["combination_coverage"] is None
    assert final_workflow["strategy_proposals"] is None
    assert final_workflow["assembly_recipe"] is None


def test_semantics_v2_concurrent_continuations_do_not_duplicate_provider_execution(
    workflow_runtime, tmp_path, monkeypatch
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    executor, transport = _configure_semantics_v2_executor(database, store, service, tmp_path)
    authorized = _authorized_executor_build(service, "concurrent")
    with pytest.raises(StaleWorkflowVersion):
        service.continue_training_dataset_workflow(
            authorized.id,
            expected_version=authorized.version + 1,
            actor="executor-test",
            idempotency_key="executor-concurrent-stale",
        )
    assert transport.calls == []

    original_invoke = executor._invoke_task
    first_claimed = Event()
    release_first = Event()
    invocation_count = 0

    def delay_first(workflow_id, plan, task, running):
        nonlocal invocation_count
        invocation_count += 1
        if invocation_count == 1:
            first_claimed.set()
            assert release_first.wait(timeout=10)
        return original_invoke(workflow_id, plan, task, running)

    monkeypatch.setattr(executor, "_invoke_task", delay_first)
    result: dict[str, object] = {}

    def run_primary() -> None:
        result["snapshot"] = service.continue_training_dataset_workflow(
            authorized.id,
            expected_version=authorized.version,
            actor="executor-test",
            idempotency_key="executor-concurrent-primary",
        )

    primary = Thread(target=run_primary)
    primary.start()
    assert first_claimed.wait(timeout=10)
    secondary = service.continue_training_dataset_workflow(
        authorized.id,
        expected_version=authorized.version,
        actor="executor-test",
        idempotency_key="executor-concurrent-secondary",
    )
    assert secondary.current_stage is WorkflowState.DISCOVERING_SOURCE_CANDIDATES
    assert invocation_count == 1
    assert transport.calls == []
    release_first.set()
    primary.join(timeout=30)
    assert not primary.is_alive()
    primary_snapshot = result["snapshot"]
    assert primary_snapshot.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES
    assert invocation_count == 5


def test_production_preapproval_resources_fit_the_default_source_boundary() -> None:
    registry = load_preapproval_provider_registry(REPO_ROOT)

    assert all(
        resource.maximum_response_bytes <= 5_000_000
        for release in registry.releases
        for resource in release.resources
    )


def test_production_search_hydration_is_complete_disk_backed_and_replayable(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    store.maximum_bytes = 20 * 1024 * 1024
    workflow_id = _workflow_id(service)
    transport = HydrationTransport()
    provider = PreapprovalMetadataProvider(
        load_preapproval_provider_registry(REPO_ROOT),
        ProviderTaskExecutor(
            transport=transport,
            cache=SourceResponseCache(database),
            artifacts=store,
        ),
    )
    cases = {
        "tox21": (EvidenceRole.ACTIVITY, "tox21-pubchem-reviewed-current"),
        "pubchem-bioassay": (EvidenceRole.ACTIVITY, "pubchem-bioassay-reviewed-current"),
        "ncbi-geo": (EvidenceRole.TRANSCRIPTOMIC, "ncbi-geo-reviewed-current"),
    }
    outputs = {}
    for ordinal, (provider_name, (role, release_id)) in enumerate(cases.items(), start=1):
        outputs[provider_name] = provider.execute(
            provider_name,
            ProviderMetadataExecutionInput(
                ledger_record=_record(provider_name, role, ordinal),
                release_id=release_id,
                query=ProviderMetadataQuery(
                    biological_target="example receptor",
                    modality="binding" if role is EvidenceRole.ACTIVITY else None,
                ),
                validation_scope="bounded_smoke",
                maximum_hydration_candidates=10,
            ),
            _invocation(workflow_id, provider_name),
        )

    assert len(transport.calls) == 20
    for output in outputs.values():
        assert output.completion_proof.completed
        assert output.completion_proof.cursor_exhausted
        assert not output.completion_proof.safety_truncated
        assert output.completion_proof.discovered_candidate_count == 2
        assert output.completion_proof.hydrated_candidate_count == 2
        assert not output.completion_proof.structural_validation_codes
        assert output.dataset_manifest is not None
        assert output.dataset_manifest.sqlite_artifact

    for provider_name in ("tox21", "pubchem-bioassay"):
        output = outputs[provider_name]
        assert output.completion_proof.activity_availability_attempt_count == 2
        assert output.completion_proof.relationship_attempt_count == 2
        assert output.dataset_manifest.counts.assay_annotations >= 2
        assert output.dataset_manifest.counts.activity_records == 4
        assert output.dataset_manifest.counts.assay_relationships == 2
        assert output.dataset_manifest.counts.compound_mappings >= 2

    geo = outputs["ncbi-geo"]
    assert geo.completion_proof.resolved_geo_accession_count == 2
    assert geo.completion_proof.geo_soft_attempt_count == 2
    assert geo.dataset_manifest.counts.transcriptomic_records >= 2
    with _open_sqlite(store, geo.dataset_manifest.sqlite_artifact) as connection:
        outcomes = {
            row[0] for row in connection.execute("SELECT outcome FROM transcriptomic_records")
        }
    assert "verified_compound_perturbation" in outcomes

    cold_fingerprints = {
        name: output.dataset_manifest.bundle_fingerprint for name, output in outputs.items()
    }
    cold_calls = list(transport.calls)
    for ordinal, (provider_name, (role, release_id)) in enumerate(cases.items(), start=1):
        replay = provider.execute(
            provider_name,
            ProviderMetadataExecutionInput(
                ledger_record=_record(provider_name, role, ordinal),
                release_id=release_id,
                query=ProviderMetadataQuery(
                    biological_target="example receptor",
                    modality="binding" if role is EvidenceRole.ACTIVITY else None,
                ),
                validation_scope="bounded_smoke",
                maximum_hydration_candidates=10,
            ),
            _invocation(workflow_id, provider_name),
        )
        assert replay.cache_only_replay
        assert not replay.normalization_ran
        assert replay.dataset_manifest.bundle_fingerprint == cold_fingerprints[provider_name]
    assert transport.calls == cold_calls


def test_registered_capabilities_match_live_validated_readiness() -> None:
    registry = load_operational_capability_registry(REPO_ROOT)
    expected = {
        "lincs-l1000",
        "toxcast",
        "tox21",
        "pubchem-bioassay",
        "pubchem-compound",
        "ncbi-geo",
        "ncbi-supporting-metadata",
    }
    assert expected <= {item.provider for item in registry.providers}
    for provider in expected:
        profile: ProviderOperationalProfile = registry.by_provider(provider)
        for declaration in profile.capabilities:
            if declaration.capability is OperationalCapability.HEAVY_EXPRESSION_EXTRACTION:
                assert not declaration.satisfies_preapproval_requirement
            else:
                assert declaration.satisfies_preapproval_requirement, (
                    provider,
                    declaration.capability,
                )
