from __future__ import annotations

import hashlib
import io
import time
import zipfile

import httpx
import pytest

from endoscan_workflows.contracts import EndpointBuildCreate, WorkflowKind
from endoscan_workflows.selective_remote_zip import (
    BoundedRangeResponse,
    HttpxBoundedRangeTransport,
    RangeRequestEvidence,
    RemoteZipAccessError,
    RemoteZipLimits,
    SelectiveRemoteZipService,
)

HOST = "release.example.invalid"
LOCATOR = f"https://{HOST}/invitrodb-summary.zip"


def _archive(
    *, unsafe: bool = False, compression: int = zipfile.ZIP_DEFLATED
) -> tuple[bytes, dict[str, bytes]]:
    members = {
        ("../escape.csv" if unsafe else "tables/mc4_all_model_fits.csv"): b"chid,aeid\n1,2\n",
        "tables/mc5-6_winning_model_fits-flags.csv": b"dtxsid,aeid,hitc\nDTXSID1,2,1\n",
        "tables/sc1_sc2.xlsx": b"small-xlsx-fixture",
        "README.txt": b"Reviewed fixture roles: mc4 catalogue; mc5-6 and sc1_sc2 activity.",
    }
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=compression) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return target.getvalue(), members


class MemoryRangeTransport:
    def __init__(self, content: bytes, *, etag: str = '"fixture-v1"') -> None:
        self.content = content
        self.etag = etag
        self.calls: list[tuple[str, int | None, int | None]] = []

    def _evidence(
        self, method: str, content: bytes, *, start: int | None = None, end: int | None = None
    ) -> RangeRequestEvidence:
        return RangeRequestEvidence(
            method=method,
            requested_range=(None if start is None else f"bytes={start}-{end}"),
            http_status=200 if method == "HEAD" else 206,
            final_locator=LOCATOR,
            final_approved_host=HOST,
            redirect_count=0,
            content_type="application/zip",
            content_range=(None if start is None else f"bytes {start}-{end}/{len(self.content)}"),
            response_bytes=len(content),
            response_sha256=hashlib.sha256(content).hexdigest(),
            etag=self.etag,
            last_modified="Tue, 01 Jul 2025 12:00:00 GMT",
            duration_ms=1,
        )

    def head(self, locator: str, **_kwargs) -> BoundedRangeResponse:
        assert locator == LOCATOR
        self.calls.append(("HEAD", None, None))
        return BoundedRangeResponse(content=b"", evidence=self._evidence("HEAD", b""))

    def get_range(
        self,
        locator: str,
        *,
        start: int,
        end: int,
        expected_size: int,
        maximum_bytes: int,
        **_kwargs,
    ) -> BoundedRangeResponse:
        assert locator == LOCATOR
        assert expected_size == len(self.content)
        assert end - start + 1 <= maximum_bytes
        content = self.content[start : end + 1]
        self.calls.append(("GET", start, end))
        return BoundedRangeResponse(
            content=content,
            evidence=self._evidence("GET", content, start=start, end=end),
        )


class CorruptingMemoryRangeTransport(MemoryRangeTransport):
    def get_range(self, locator: str, **kwargs) -> BoundedRangeResponse:
        response = super().get_range(locator, **kwargs)
        if len(self.calls) >= 6 and len(response.content) > 0:
            corrupted = bytes([response.content[0] ^ 1]) + response.content[1:]
            return BoundedRangeResponse(
                content=corrupted,
                evidence=response.evidence.model_copy(
                    update={
                        "response_sha256": hashlib.sha256(corrupted).hexdigest(),
                    }
                ),
            )
        return response


def _workflow(service, suffix: str) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name=f"Selective remote ZIP {suffix}",
            endpoint_slug=f"selective-remote-zip-{suffix}",
            biological_goal="Validate technical reviewed range access only.",
            created_by="preapproval-test",
            idempotency_key=f"selective-remote-zip-{suffix}",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
        )
    ).id


def test_member_manifest_selective_extraction_and_zero_request_replay(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "complete")
    content, source_members = _archive()
    transport = MemoryRangeTransport(content)
    selective = SelectiveRemoteZipService(transport=transport, artifacts=store)

    manifest, manifest_artifact, cache_hit = selective.inspect(
        workflow_id=workflow_id,
        locator=LOCATOR,
        expected_size=len(content),
        approved_hosts=frozenset({HOST}),
        release_id="fixture-v1",
        release_provenance=["offline reviewed fixture"],
    )

    assert not cache_hit
    assert manifest_artifact.artifact_type == "remote_zip_member_manifest"
    assert manifest.member_count == len(source_members)
    assert manifest.source_request_count == 3
    assert manifest.downloaded_bytes == 1 + len(content) + manifest.central_directory_size
    assert transport.calls[0] == ("GET", 0, 0)
    activity_name = "tables/mc5-6_winning_model_fits-flags.csv"
    activity = next(item for item in manifest.members if item.member_name == activity_name)
    assert activity.inferred_scientific_role == "multi_concentration_activity_hit_calls"
    assert activity.role_verification_status == "unverified_filename_hint"
    with pytest.raises(RemoteZipAccessError, match="not verified"):
        selective.extract_member(
            workflow_id=workflow_id,
            manifest=manifest,
            member_name=activity_name,
            approved_hosts=frozenset({HOST}),
        )

    verified = selective.verify_roles(
        manifest,
        exact_roles={activity_name: "multi_concentration_activity_hit_calls"},
        verification_basis=["README.txt SHA-256 fixture"],
    )
    extracted = selective.extract_member(
        workflow_id=workflow_id,
        manifest=verified,
        member_name=activity_name,
        approved_hosts=frozenset({HOST}),
    )
    assert not extracted.cache_hit
    assert extracted.source_request_count == 3
    descriptor, path = store.verified_path(extracted.artifact.artifact_id)
    assert path.read_bytes() == source_members[activity_name]
    assert descriptor.sha256 == hashlib.sha256(source_members[activity_name]).hexdigest()

    calls_before_replay = list(transport.calls)
    replay_manifest, replay_artifact, replay_cache_hit = selective.inspect(
        workflow_id=workflow_id,
        locator=LOCATOR,
        expected_size=len(content),
        approved_hosts=frozenset({HOST}),
        release_id="fixture-v1",
        release_provenance=["offline reviewed fixture"],
    )
    replay_member = selective.extract_member(
        workflow_id=workflow_id,
        manifest=verified,
        member_name=activity_name,
        approved_hosts=frozenset({HOST}),
    )
    assert replay_cache_hit
    assert replay_artifact == manifest_artifact
    assert replay_manifest.manifest_fingerprint == manifest.manifest_fingerprint
    assert replay_member.cache_hit
    assert replay_member.source_request_count == 0
    assert transport.calls == calls_before_replay


def test_unsafe_member_path_stops_before_manifest_persistence(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "path-traversal")
    content, _ = _archive(unsafe=True)
    selective = SelectiveRemoteZipService(transport=MemoryRangeTransport(content), artifacts=store)
    with pytest.raises(RemoteZipAccessError) as captured:
        selective.inspect(
            workflow_id=workflow_id,
            locator=LOCATOR,
            expected_size=len(content),
            approved_hosts=frozenset({HOST}),
            release_id="fixture-v1",
            release_provenance=["offline reviewed fixture"],
        )
    assert captured.value.code == "REMOTE_ZIP_UNSAFE_MEMBER_PATH"
    assert store.find_by_logical_name(workflow_id, "remote-zip-members") is None


def test_encrypted_unsupported_and_oversized_members_are_rejected(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "member-policy")
    content, _ = _archive()
    selective = SelectiveRemoteZipService(
        transport=MemoryRangeTransport(content),
        artifacts=store,
        limits=RemoteZipLimits(maximum_compressed_member_bytes=1_024),
    )
    manifest, _, _ = selective.inspect(
        workflow_id=workflow_id,
        locator=LOCATOR,
        expected_size=len(content),
        approved_hosts=frozenset({HOST}),
        release_id="fixture-v1",
        release_provenance=["offline reviewed fixture"],
    )
    selected = manifest.members[0]
    for update, expected_code in (
        ({"flag_bits": selected.flag_bits | 1}, "REMOTE_ZIP_ENCRYPTED_MEMBER"),
        ({"compression_method": 99}, "REMOTE_ZIP_UNSUPPORTED_COMPRESSION"),
        ({"compressed_size": 2_000}, "REMOTE_ZIP_COMPRESSED_MEMBER_LIMIT"),
    ):
        altered = manifest.model_copy(
            update={
                "members": [selected.model_copy(update=update), *manifest.members[1:]],
                "manifest_fingerprint": "0" * 64,
            }
        )
        altered = altered.model_copy(
            update={
                "manifest_fingerprint": hashlib.sha256(
                    str(time.monotonic_ns()).encode()
                ).hexdigest()
            }
        )
        # Extraction policy is evaluated before the manifest is serialized again.
        object.__setattr__(altered, "manifest_fingerprint", manifest.manifest_fingerprint)
        verified_member = altered.members[0].model_copy(
            update={
                "role_verification_status": "verified_release_documentation",
                "role_verification_basis": ["fixture README"],
            }
        )
        object.__setattr__(altered, "members", [verified_member, *altered.members[1:]])
        with pytest.raises(RemoteZipAccessError) as captured:
            selective.extract_member(
                workflow_id=workflow_id,
                manifest=altered,
                member_name=verified_member.member_name,
                approved_hosts=frozenset({HOST}),
            )
        assert captured.value.code == expected_code


def test_http_transport_rejects_ignored_range_before_accepting_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=0-9"
        return httpx.Response(
            200,
            headers={"content-type": "application/zip", "content-length": "9999999"},
            content=b"not-a-partial-response",
        )

    with HttpxBoundedRangeTransport(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(RemoteZipAccessError) as captured:
            transport.get_range(
                LOCATOR,
                start=0,
                end=9,
                expected_size=100,
                expected_etag=None,
                expected_last_modified="Tue, 01 Jul 2025 12:00:00 GMT",
                approved_hosts=frozenset({HOST}),
                maximum_bytes=10,
                maximum_redirects=0,
            )
    assert captured.value.code == "PUBLIC_PROVIDER_RANGE_UNAVAILABLE"


def test_http_transport_rejects_disallowed_redirect_and_mime() -> None:
    def redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.invalid/archive.zip"})

    with HttpxBoundedRangeTransport(transport=httpx.MockTransport(redirect_handler)) as transport:
        with pytest.raises(RemoteZipAccessError) as captured:
            transport.head(LOCATOR, approved_hosts=frozenset({HOST}), maximum_redirects=1)
    assert captured.value.code == "REMOTE_ZIP_URL_POLICY"

    def mime_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html", "etag": '"x"'})

    with HttpxBoundedRangeTransport(transport=httpx.MockTransport(mime_handler)) as transport:
        with pytest.raises(RemoteZipAccessError) as captured:
            transport.head(LOCATOR, approved_hosts=frozenset({HOST}), maximum_redirects=0)
    assert captured.value.code == "REMOTE_ZIP_MIME_MISMATCH"


def test_member_crc_and_archive_identity_changes_are_rejected(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "crc")
    content, _ = _archive(compression=zipfile.ZIP_STORED)
    transport = CorruptingMemoryRangeTransport(content)
    selective = SelectiveRemoteZipService(transport=transport, artifacts=store)
    manifest, _, _ = selective.inspect(
        workflow_id=workflow_id,
        locator=LOCATOR,
        expected_size=len(content),
        approved_hosts=frozenset({HOST}),
        release_id="fixture-v1",
        release_provenance=["offline reviewed fixture"],
    )
    name = "tables/mc5-6_winning_model_fits-flags.csv"
    verified = selective.verify_roles(
        manifest,
        exact_roles={name: "multi_concentration_activity_hit_calls"},
        verification_basis=["fixture README"],
    )
    with pytest.raises(RemoteZipAccessError) as captured:
        selective.extract_member(
            workflow_id=workflow_id,
            manifest=verified,
            member_name=name,
            approved_hosts=frozenset({HOST}),
        )
    assert captured.value.code == "REMOTE_ZIP_CRC_MISMATCH"

    def changed_etag_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={
                "content-type": "application/zip",
                "content-range": "bytes 0-0/100",
                "content-length": "1",
                "etag": '"changed"',
            },
            content=b"P",
            request=request,
        )

    with HttpxBoundedRangeTransport(
        transport=httpx.MockTransport(changed_etag_handler)
    ) as http_transport:
        with pytest.raises(RemoteZipAccessError) as captured:
            http_transport.get_range(
                LOCATOR,
                start=0,
                end=0,
                expected_size=100,
                expected_etag='"original"',
                expected_last_modified=None,
                approved_hosts=frozenset({HOST}),
                maximum_bytes=1,
                maximum_redirects=0,
            )
    assert captured.value.code == "REMOTE_ZIP_UNSTABLE_IDENTITY"
