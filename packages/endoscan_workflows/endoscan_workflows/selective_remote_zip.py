"""Bounded, reviewed HTTP-Range access to immutable public ZIP releases.

The implementation never treats a filename hint as a verified scientific role.  It
only reads the ZIP directory and explicitly selected members, and refuses to fall
back to a complete archive download when byte ranges are unavailable.
"""

from __future__ import annotations

import binascii
import hashlib
import struct
import time
import zlib
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import Field, model_validator

from .artifacts import LocalArtifactStore
from .discovery_strategy import ArtifactReference, ImmutableV2Contract, deterministic_fingerprint

EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
CENTRAL_SIGNATURE = b"PK\x01\x02"
LOCAL_SIGNATURE = b"PK\x03\x04"
MAXIMUM_EOCD_WINDOW = 65_557


class RemoteZipAccessError(RuntimeError):
    """Safe, classified selective-access failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RangeRequestEvidence(ImmutableV2Contract):
    method: Literal["HEAD", "GET"]
    requested_range: str | None = None
    http_status: int = Field(ge=100, le=599)
    final_locator: str = Field(pattern=r"^https://[^\s]+$")
    final_approved_host: str
    redirect_count: int = Field(ge=0, le=3)
    content_type: str | None = None
    content_length: int | None = Field(default=None, ge=0)
    accept_ranges: str | None = None
    content_range: str | None = None
    response_bytes: int = Field(ge=0)
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    etag: str | None = None
    last_modified: str | None = None
    duration_ms: int = Field(ge=0)


class BoundedRangeResponse(ImmutableV2Contract):
    content: bytes = Field(exclude=True)
    evidence: RangeRequestEvidence


class BoundedRangeTransport(Protocol):
    def head(
        self,
        locator: str,
        *,
        approved_hosts: frozenset[str],
        maximum_redirects: int,
    ) -> BoundedRangeResponse: ...

    def get_range(
        self,
        locator: str,
        *,
        start: int,
        end: int,
        expected_size: int,
        expected_etag: str | None,
        expected_last_modified: str | None,
        approved_hosts: frozenset[str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> BoundedRangeResponse: ...


class HttpxBoundedRangeTransport:
    """Streaming transport that aborts before reading an ignored Range response."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        user_agent: str = "EndoScan/reviewed-range-access",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/zip, application/octet-stream",
            },
            transport=transport,
        )
        self.evidence: list[RangeRequestEvidence] = []
        self.safe_failures: list[dict[str, object]] = []

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HttpxBoundedRangeTransport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _approved(locator: str, approved_hosts: frozenset[str]) -> str:
        parsed = urlparse(locator)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or host not in approved_hosts:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_URL_POLICY",
                "Remote ZIP locator is outside the reviewed HTTPS host allowlist.",
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise RemoteZipAccessError("REMOTE_ZIP_URL_POLICY", "Remote ZIP locator is unsafe.")
        return host

    def _request(
        self,
        method: Literal["HEAD", "GET"],
        locator: str,
        *,
        headers: Mapping[str, str],
        approved_hosts: frozenset[str],
        maximum_bytes: int,
        maximum_redirects: int,
        require_partial: bool,
    ) -> BoundedRangeResponse:
        current = locator
        redirects = 0
        started = time.monotonic()
        while True:
            host = self._approved(current, approved_hosts)
            request = self.client.build_request(method, current, headers=dict(headers))
            try:
                response = self.client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                self.safe_failures.append(
                    {
                        "method": method,
                        "requested_range": headers.get("Range"),
                        "locator": current,
                        "safe_error_code": "PUBLIC_PROVIDER_RANGE_TIMEOUT",
                        "exception_class": type(exc).__name__,
                        "request_left_process": True,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                    }
                )
                raise RemoteZipAccessError(
                    "PUBLIC_PROVIDER_RANGE_TIMEOUT",
                    "The public archive range request timed out within the reviewed bound.",
                ) from exc
            except httpx.HTTPError as exc:
                self.safe_failures.append(
                    {
                        "method": method,
                        "requested_range": headers.get("Range"),
                        "locator": current,
                        "safe_error_code": "PUBLIC_PROVIDER_RANGE_UNAVAILABLE",
                        "exception_class": type(exc).__name__,
                        "request_left_process": True,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                    }
                )
                raise RemoteZipAccessError(
                    "PUBLIC_PROVIDER_RANGE_UNAVAILABLE",
                    "The public archive range request was unavailable within policy.",
                ) from exc
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirects >= maximum_redirects:
                        raise RemoteZipAccessError(
                            "REMOTE_ZIP_REDIRECT_POLICY",
                            "Remote ZIP redirect exceeded the reviewed policy.",
                        )
                    current = urljoin(current, location)
                    self._approved(current, approved_hosts)
                    redirects += 1
                    continue
                if require_partial and response.status_code != 206:
                    self.safe_failures.append(
                        {
                            "method": method,
                            "requested_range": headers.get("Range"),
                            "locator": str(response.url),
                            "safe_error_code": "PUBLIC_PROVIDER_RANGE_UNAVAILABLE",
                            "http_status": response.status_code,
                            "content_type": response.headers.get("content-type"),
                            "content_length": response.headers.get("content-length"),
                            "request_left_process": True,
                            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                        }
                    )
                    raise RemoteZipAccessError(
                        "PUBLIC_PROVIDER_RANGE_UNAVAILABLE",
                        "The public archive did not honor the bounded byte-range request.",
                    )
                if not require_partial and not 200 <= response.status_code < 300:
                    raise RemoteZipAccessError(
                        "PUBLIC_PROVIDER_ARCHIVE_METADATA_UNAVAILABLE",
                        "The public archive metadata request was not successful.",
                    )
                content_length = response.headers.get("content-length")
                if method == "GET" and content_length:
                    try:
                        declared = int(content_length)
                    except ValueError as exc:
                        raise RemoteZipAccessError(
                            "REMOTE_ZIP_INVALID_RESPONSE", "Remote ZIP Content-Length is invalid."
                        ) from exc
                    if declared > maximum_bytes:
                        raise RemoteZipAccessError(
                            "REMOTE_ZIP_RANGE_TOO_LARGE",
                            "Remote ZIP range response exceeds the reviewed byte limit.",
                        )
                content = bytearray()
                if method == "GET":
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > maximum_bytes:
                            raise RemoteZipAccessError(
                                "REMOTE_ZIP_RANGE_TOO_LARGE",
                                "Remote ZIP range response exceeded the reviewed byte limit.",
                            )
                media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if media_type and media_type not in {
                    "application/zip",
                    "application/octet-stream",
                    "application/x-zip-compressed",
                }:
                    raise RemoteZipAccessError(
                        "REMOTE_ZIP_MIME_MISMATCH",
                        "Remote ZIP endpoint returned an unexpected MIME type.",
                    )
                evidence = RangeRequestEvidence(
                    method=method,
                    requested_range=headers.get("Range"),
                    http_status=response.status_code,
                    final_locator=str(response.url),
                    final_approved_host=host,
                    redirect_count=redirects,
                    content_type=media_type or None,
                    content_length=(
                        int(response.headers["content-length"])
                        if response.headers.get("content-length", "").isdigit()
                        else None
                    ),
                    accept_ranges=response.headers.get("accept-ranges"),
                    content_range=response.headers.get("content-range"),
                    response_bytes=len(content),
                    response_sha256=hashlib.sha256(content).hexdigest(),
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
                self.evidence.append(evidence)
                return BoundedRangeResponse(content=bytes(content), evidence=evidence)
            finally:
                response.close()

    def head(
        self,
        locator: str,
        *,
        approved_hosts: frozenset[str],
        maximum_redirects: int,
    ) -> BoundedRangeResponse:
        return self._request(
            "HEAD",
            locator,
            headers={},
            approved_hosts=approved_hosts,
            maximum_bytes=0,
            maximum_redirects=maximum_redirects,
            require_partial=False,
        )

    def get_range(
        self,
        locator: str,
        *,
        start: int,
        end: int,
        expected_size: int,
        expected_etag: str | None,
        expected_last_modified: str | None,
        approved_hosts: frozenset[str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> BoundedRangeResponse:
        if start < 0 or end < start or end >= expected_size or end - start + 1 > maximum_bytes:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_RANGE_POLICY", "Remote ZIP byte range is invalid."
            )
        headers = {"Range": f"bytes={start}-{end}"}
        if expected_etag and not expected_etag.startswith("W/"):
            headers["If-Range"] = expected_etag
        elif expected_last_modified:
            headers["If-Range"] = expected_last_modified
        response = self._request(
            "GET",
            locator,
            headers=headers,
            approved_hosts=approved_hosts,
            maximum_bytes=maximum_bytes,
            maximum_redirects=maximum_redirects,
            require_partial=True,
        )
        expected_content_range = f"bytes {start}-{end}/{expected_size}"
        if response.evidence.content_range != expected_content_range:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_CONTENT_RANGE",
                "Remote ZIP response did not match the exact requested range.",
            )
        if len(response.content) != end - start + 1:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_TRUNCATED_RANGE", "Remote ZIP response was shorter than requested."
            )
        if expected_etag and response.evidence.etag not in {None, expected_etag}:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_UNSTABLE_IDENTITY", "Remote ZIP ETag changed during retrieval."
            )
        if expected_last_modified and response.evidence.last_modified not in {
            None,
            expected_last_modified,
        }:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_UNSTABLE_IDENTITY",
                "Remote ZIP Last-Modified changed during retrieval.",
            )
        return response


class RemoteZipLimits(ImmutableV2Contract):
    maximum_range_requests: int = Field(default=8, ge=3, le=20)
    maximum_central_directory_bytes: int = Field(default=32 * 1024 * 1024, ge=65_557)
    maximum_compressed_member_bytes: int = Field(default=64 * 1024 * 1024, ge=1_024)
    maximum_uncompressed_member_bytes: int = Field(default=256 * 1024 * 1024, ge=1_024)
    maximum_decompression_ratio: float = Field(default=200, ge=1, le=1_000)
    maximum_wall_time_seconds: float = Field(default=180, gt=0, le=1_200)
    maximum_temporary_disk_bytes: int = Field(default=512 * 1024 * 1024, ge=1_024)
    maximum_members: int = Field(default=100_000, ge=1, le=1_000_000)
    maximum_redirects: int = Field(default=2, ge=0, le=3)


class RemoteZipArchiveIdentity(ImmutableV2Contract):
    locator: str = Field(pattern=r"^https://[^\s]+$")
    size_bytes: int = Field(ge=22)
    etag: str | None = None
    last_modified: str | None = None
    final_approved_host: str

    @model_validator(mode="after")
    def stable_version_present(self) -> RemoteZipArchiveIdentity:
        if not self.etag and not self.last_modified:
            raise ValueError("remote ZIP identity requires ETag or Last-Modified")
        return self


class RemoteZipMember(ImmutableV2Contract):
    member_name: str
    compressed_size: int = Field(ge=0)
    uncompressed_size: int = Field(ge=0)
    compression_method: int = Field(ge=0, le=65_535)
    crc32: str = Field(pattern=r"^[a-f0-9]{8}$")
    local_header_offset: int = Field(ge=0)
    flag_bits: int = Field(ge=0, le=65_535)
    inferred_scientific_role: str | None = None
    role_verification_status: Literal[
        "not_applicable", "unverified_filename_hint", "verified_release_documentation"
    ]
    role_verification_basis: list[str] = Field(default_factory=list, max_length=20)


class RemoteZipMemberManifest(ImmutableV2Contract):
    release_id: str
    release_provenance: list[str] = Field(min_length=1, max_length=20)
    archive: RemoteZipArchiveIdentity
    central_directory_offset: int = Field(ge=0)
    central_directory_size: int = Field(ge=1)
    member_count: int = Field(ge=1)
    members: list[RemoteZipMember] = Field(min_length=1)
    range_evidence: list[RangeRequestEvidence] = Field(min_length=2, max_length=20)
    source_request_count: int = Field(ge=2, le=20)
    downloaded_bytes: int = Field(ge=1)
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fingerprint_matches(self) -> RemoteZipMemberManifest:
        payload = self.model_dump(mode="json", exclude={"manifest_fingerprint"})
        if deterministic_fingerprint(payload) != self.manifest_fingerprint:
            raise ValueError("remote ZIP member-manifest fingerprint does not match")
        if self.member_count != len(self.members):
            raise ValueError("remote ZIP member count does not match")
        return self

    @classmethod
    def create(cls, **values: object) -> RemoteZipMemberManifest:
        values["manifest_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


class SelectiveZipMemberResult(ImmutableV2Contract):
    member: RemoteZipMember
    artifact: ArtifactReference
    source_request_count: int = Field(ge=0, le=3)
    downloaded_bytes: int = Field(ge=0)
    cache_hit: bool
    range_evidence: list[RangeRequestEvidence] = Field(default_factory=list, max_length=3)


def _safe_member_name(raw: bytes, flags: int) -> str:
    name = raw.decode("utf-8" if flags & 0x800 else "cp437")
    path = PurePosixPath(name)
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RemoteZipAccessError("REMOTE_ZIP_UNSAFE_MEMBER_PATH", "ZIP member path is unsafe.")
    return name


def _zip64_values(extra: bytes, needs: list[bool]) -> list[int | None]:
    cursor = 0
    payload: bytes | None = None
    while cursor + 4 <= len(extra):
        field_id, size = struct.unpack_from("<HH", extra, cursor)
        cursor += 4
        value = extra[cursor : cursor + size]
        cursor += size
        if field_id == 0x0001:
            payload = value
            break
    values: list[int | None] = []
    position = 0
    for needed in needs:
        if not needed:
            values.append(None)
            continue
        if payload is None or position + 8 > len(payload):
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_ZIP64", "ZIP64 extra field is incomplete."
            )
        values.append(struct.unpack_from("<Q", payload, position)[0])
        position += 8
    return values


def _role_hint(
    name: str,
) -> tuple[str | None, Literal["not_applicable", "unverified_filename_hint"]]:
    lowered = name.casefold()
    hints = (
        ("mc4_all_model_fits", "complete_tested_chemical_catalogue"),
        ("mc5-6_winning_model_fits", "multi_concentration_activity_hit_calls"),
        ("sc1_sc2", "single_concentration_activity_hit_calls"),
        ("readme", "release_documentation"),
        ("schema", "release_documentation"),
    )
    for token, role in hints:
        if token in lowered:
            return role, "unverified_filename_hint"
    return None, "not_applicable"


def _parse_central_directory(
    content: bytes, *, expected_count: int, maximum_members: int
) -> list[RemoteZipMember]:
    if expected_count < 1 or expected_count > maximum_members:
        raise RemoteZipAccessError("REMOTE_ZIP_MEMBER_LIMIT", "ZIP member count is outside policy.")
    members: list[RemoteZipMember] = []
    position = 0
    while position < len(content) and len(members) < expected_count:
        if content[position : position + 4] != CENTRAL_SIGNATURE or position + 46 > len(content):
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_CENTRAL_DIRECTORY", "ZIP central directory is malformed."
            )
        fields = struct.unpack_from("<4s6H3I5H2I", content, position)
        (
            _signature,
            _version_made,
            _version_needed,
            flags,
            method,
            _mtime,
            _mdate,
            crc,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_offset,
        ) = fields
        end = position + 46 + name_length + extra_length + comment_length
        if end > len(content):
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_CENTRAL_DIRECTORY", "ZIP directory entry is truncated."
            )
        name_raw = content[position + 46 : position + 46 + name_length]
        extra = content[position + 46 + name_length : position + 46 + name_length + extra_length]
        zip64 = _zip64_values(
            extra,
            [
                uncompressed_size == 0xFFFFFFFF,
                compressed_size == 0xFFFFFFFF,
                local_offset == 0xFFFFFFFF,
            ],
        )
        uncompressed_size = int(zip64[0] if zip64[0] is not None else uncompressed_size)
        compressed_size = int(zip64[1] if zip64[1] is not None else compressed_size)
        local_offset = int(zip64[2] if zip64[2] is not None else local_offset)
        if disk_start not in {0, 0xFFFF}:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_MULTIDISK_UNSUPPORTED", "Multi-disk ZIP is unsupported."
            )
        name = _safe_member_name(name_raw, flags)
        role, status = _role_hint(name)
        members.append(
            RemoteZipMember(
                member_name=name,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                compression_method=method,
                crc32=f"{crc:08x}",
                local_header_offset=local_offset,
                flag_bits=flags,
                inferred_scientific_role=role,
                role_verification_status=status,
            )
        )
        position = end
    if len(members) != expected_count:
        raise RemoteZipAccessError(
            "REMOTE_ZIP_INVALID_CENTRAL_DIRECTORY", "ZIP member enumeration is incomplete."
        )
    offsets = [item.local_header_offset for item in members]
    if len(offsets) != len(set(offsets)):
        raise RemoteZipAccessError("REMOTE_ZIP_OVERLAPPING_RANGES", "ZIP member offsets overlap.")
    return members


class SelectiveRemoteZipService:
    def __init__(
        self,
        *,
        transport: BoundedRangeTransport,
        artifacts: LocalArtifactStore,
        limits: RemoteZipLimits | None = None,
    ) -> None:
        self.transport = transport
        self.artifacts = artifacts
        self.limits = limits or RemoteZipLimits()

    def inspect(
        self,
        *,
        workflow_id: str,
        locator: str,
        expected_size: int,
        approved_hosts: frozenset[str],
        release_id: str,
        release_provenance: list[str],
    ) -> tuple[RemoteZipMemberManifest, ArtifactReference, bool]:
        started = time.monotonic()
        cache_identity = deterministic_fingerprint(
            {
                "locator": locator,
                "expected_size": expected_size,
                "release_id": release_id,
                "release_provenance": release_provenance,
                "limits": self.limits.model_dump(mode="json"),
            }
        )
        logical_name = f"remote-zip-members-{cache_identity}.json"
        if cached := self.artifacts.find_by_logical_name(workflow_id, logical_name):
            _, content = self.artifacts.get(cached.id)
            manifest = RemoteZipMemberManifest.model_validate_json(content)
            return (
                manifest,
                ArtifactReference(
                    artifact_id=cached.id,
                    sha256=cached.sha256,
                    artifact_type=cached.artifact_type,
                ),
                True,
            )
        # A one-byte Range request is the reviewed metadata probe.  It proves the
        # object length through Content-Range and avoids relying on servers that
        # reject HEAD for otherwise public download endpoints.
        probe = self.transport.get_range(
            locator,
            start=0,
            end=0,
            expected_size=expected_size,
            expected_etag=None,
            expected_last_modified=None,
            approved_hosts=approved_hosts,
            maximum_bytes=1,
            maximum_redirects=self.limits.maximum_redirects,
        )
        size = expected_size
        if size < 22:
            raise RemoteZipAccessError("REMOTE_ZIP_INVALID_SIZE", "ZIP archive size is invalid.")
        identity = RemoteZipArchiveIdentity(
            locator=locator,
            size_bytes=size,
            etag=probe.evidence.etag,
            last_modified=probe.evidence.last_modified,
            final_approved_host=probe.evidence.final_approved_host,
        )
        tail_start = max(0, size - MAXIMUM_EOCD_WINDOW)
        tail = self.transport.get_range(
            locator,
            start=tail_start,
            end=size - 1,
            expected_size=size,
            expected_etag=identity.etag,
            expected_last_modified=identity.last_modified,
            approved_hosts=approved_hosts,
            maximum_bytes=MAXIMUM_EOCD_WINDOW,
            maximum_redirects=self.limits.maximum_redirects,
        )
        evidence = [probe.evidence, tail.evidence]
        eocd_position = tail.content.rfind(EOCD_SIGNATURE)
        if eocd_position < 0 or eocd_position + 22 > len(tail.content):
            raise RemoteZipAccessError("REMOTE_ZIP_EOCD_MISSING", "ZIP end record was not found.")
        (
            _signature,
            disk_number,
            central_disk,
            entries_on_disk,
            entries_total,
            central_size,
            central_offset,
            comment_length,
        ) = struct.unpack_from("<4s4H2IH", tail.content, eocd_position)
        if disk_number or central_disk or entries_on_disk != entries_total:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_MULTIDISK_UNSUPPORTED", "Multi-disk ZIP is unsupported."
            )
        if eocd_position + 22 + comment_length > len(tail.content):
            raise RemoteZipAccessError("REMOTE_ZIP_EOCD_TRUNCATED", "ZIP end record is truncated.")
        if entries_total == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
            locator_position = eocd_position - 20
            if (
                locator_position < 0
                or tail.content[locator_position : locator_position + 4] != ZIP64_LOCATOR_SIGNATURE
            ):
                raise RemoteZipAccessError("REMOTE_ZIP_INVALID_ZIP64", "ZIP64 locator is absent.")
            _sig, zip64_disk, zip64_offset, total_disks = struct.unpack_from(
                "<4sIQI", tail.content, locator_position
            )
            if zip64_disk or total_disks != 1:
                raise RemoteZipAccessError(
                    "REMOTE_ZIP_MULTIDISK_UNSUPPORTED", "Multi-disk ZIP64 is unsupported."
                )
            zip64 = self.transport.get_range(
                locator,
                start=zip64_offset,
                end=zip64_offset + 55,
                expected_size=size,
                expected_etag=identity.etag,
                expected_last_modified=identity.last_modified,
                approved_hosts=approved_hosts,
                maximum_bytes=56,
                maximum_redirects=self.limits.maximum_redirects,
            )
            evidence.append(zip64.evidence)
            if zip64.content[:4] != ZIP64_EOCD_SIGNATURE:
                raise RemoteZipAccessError(
                    "REMOTE_ZIP_INVALID_ZIP64", "ZIP64 end record is invalid."
                )
            unpacked = struct.unpack_from("<4sQ2H2I4Q", zip64.content, 0)
            entries_total = int(unpacked[7])
            central_size = int(unpacked[8])
            central_offset = int(unpacked[9])
        if central_size < 1 or central_size > self.limits.maximum_central_directory_bytes:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_CENTRAL_DIRECTORY_LIMIT",
                "ZIP central directory exceeds the reviewed selective-access limit.",
            )
        if central_offset + central_size > tail_start + eocd_position:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_CENTRAL_DIRECTORY", "ZIP central directory range is invalid."
            )
        central = self.transport.get_range(
            locator,
            start=central_offset,
            end=central_offset + central_size - 1,
            expected_size=size,
            expected_etag=identity.etag,
            expected_last_modified=identity.last_modified,
            approved_hosts=approved_hosts,
            maximum_bytes=self.limits.maximum_central_directory_bytes,
            maximum_redirects=self.limits.maximum_redirects,
        )
        evidence.append(central.evidence)
        source_request_count = sum(1 + item.redirect_count for item in evidence)
        if source_request_count > self.limits.maximum_range_requests:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_REQUEST_LIMIT", "ZIP range-request limit exceeded."
            )
        members = _parse_central_directory(
            central.content,
            expected_count=int(entries_total),
            maximum_members=self.limits.maximum_members,
        )
        if time.monotonic() - started > self.limits.maximum_wall_time_seconds:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_WALL_TIME_LIMIT", "ZIP inspection exceeded its wall-time limit."
            )
        manifest = RemoteZipMemberManifest.create(
            release_id=release_id,
            release_provenance=release_provenance,
            archive=identity,
            central_directory_offset=int(central_offset),
            central_directory_size=int(central_size),
            member_count=len(members),
            members=members,
            range_evidence=evidence,
            source_request_count=source_request_count,
            downloaded_bytes=sum(item.response_bytes for item in evidence),
        )
        content = manifest.model_dump_json(indent=2).encode()
        descriptor = self.artifacts.put_bytes(
            workflow_id=workflow_id,
            content=content,
            mime_type="application/json",
            artifact_type="remote_zip_member_manifest",
            logical_name=logical_name,
            producer="selective-remote-zip",
            idempotency_key=cache_identity,
            original_source=locator,
        )
        return (
            manifest,
            ArtifactReference(
                artifact_id=descriptor.id,
                sha256=descriptor.sha256,
                artifact_type=descriptor.artifact_type,
            ),
            False,
        )

    @staticmethod
    def verify_roles(
        manifest: RemoteZipMemberManifest,
        *,
        exact_roles: Mapping[str, str],
        verification_basis: list[str],
    ) -> RemoteZipMemberManifest:
        if not verification_basis:
            raise ValueError("verified ZIP roles require release-documentation provenance")
        unknown = set(exact_roles) - {item.member_name for item in manifest.members}
        if unknown:
            raise ValueError("verified ZIP role mapping contains absent members")
        members = [
            item.model_copy(
                update={
                    "inferred_scientific_role": exact_roles[item.member_name],
                    "role_verification_status": "verified_release_documentation",
                    "role_verification_basis": verification_basis,
                }
            )
            if item.member_name in exact_roles
            else item
            for item in manifest.members
        ]
        payload = manifest.model_dump(mode="json", exclude={"manifest_fingerprint", "members"})
        return RemoteZipMemberManifest.create(**payload, members=members)

    def extract_member(
        self,
        *,
        workflow_id: str,
        manifest: RemoteZipMemberManifest,
        member_name: str,
        approved_hosts: frozenset[str],
    ) -> SelectiveZipMemberResult:
        started = time.monotonic()
        member = next((item for item in manifest.members if item.member_name == member_name), None)
        if member is None:
            raise KeyError(member_name)
        if member.role_verification_status != "verified_release_documentation":
            raise RemoteZipAccessError(
                "REMOTE_ZIP_ROLE_UNVERIFIED",
                "ZIP member scientific role is not verified by release documentation.",
            )
        if member.flag_bits & 0x1:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_ENCRYPTED_MEMBER", "Encrypted ZIP member rejected."
            )
        if member.compression_method not in {0, 8}:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_UNSUPPORTED_COMPRESSION", "ZIP compression method is unsupported."
            )
        if member.compressed_size > self.limits.maximum_compressed_member_bytes:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_COMPRESSED_MEMBER_LIMIT", "Compressed ZIP member exceeds policy."
            )
        if member.uncompressed_size > self.limits.maximum_uncompressed_member_bytes:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_UNCOMPRESSED_MEMBER_LIMIT", "Uncompressed ZIP member exceeds policy."
            )
        if member.compressed_size == 0 and member.uncompressed_size:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_DECOMPRESSION_RATIO", "ZIP member ratio is unsafe."
            )
        if member.compressed_size and (
            member.uncompressed_size / member.compressed_size
            > self.limits.maximum_decompression_ratio
        ):
            raise RemoteZipAccessError(
                "REMOTE_ZIP_DECOMPRESSION_RATIO", "ZIP member ratio is unsafe."
            )
        identity = deterministic_fingerprint(
            {
                "manifest": manifest.manifest_fingerprint,
                "member": member.model_dump(mode="json"),
            }
        )
        logical_name = f"remote-zip-member-{identity}"
        if cached := self.artifacts.find_by_logical_name(workflow_id, logical_name):
            return SelectiveZipMemberResult(
                member=member,
                artifact=ArtifactReference(
                    artifact_id=cached.id,
                    sha256=cached.sha256,
                    artifact_type=cached.artifact_type,
                ),
                source_request_count=0,
                downloaded_bytes=0,
                cache_hit=True,
            )
        archive = manifest.archive
        fixed = self.transport.get_range(
            archive.locator,
            start=member.local_header_offset,
            end=member.local_header_offset + 29,
            expected_size=archive.size_bytes,
            expected_etag=archive.etag,
            expected_last_modified=archive.last_modified,
            approved_hosts=approved_hosts,
            maximum_bytes=30,
            maximum_redirects=self.limits.maximum_redirects,
        )
        if fixed.content[:4] != LOCAL_SIGNATURE:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_INVALID_LOCAL_HEADER", "ZIP local header is invalid."
            )
        fields = struct.unpack_from("<4s5H3I2H", fixed.content, 0)
        flags, method, name_length, extra_length = fields[2], fields[3], fields[9], fields[10]
        if flags != member.flag_bits or method != member.compression_method:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_HEADER_MISMATCH", "ZIP local and central headers do not agree."
            )
        variable_size = name_length + extra_length
        variable = self.transport.get_range(
            archive.locator,
            start=member.local_header_offset + 30,
            end=member.local_header_offset + 30 + variable_size - 1,
            expected_size=archive.size_bytes,
            expected_etag=archive.etag,
            expected_last_modified=archive.last_modified,
            approved_hosts=approved_hosts,
            maximum_bytes=max(variable_size, 1),
            maximum_redirects=self.limits.maximum_redirects,
        )
        if _safe_member_name(variable.content[:name_length], flags) != member.member_name:
            raise RemoteZipAccessError("REMOTE_ZIP_HEADER_MISMATCH", "ZIP member name changed.")
        data_start = member.local_header_offset + 30 + variable_size
        data_end = data_start + member.compressed_size - 1
        if data_end >= manifest.central_directory_offset:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_OVERLAPPING_RANGES", "ZIP member overlaps directory."
            )
        compressed = self.transport.get_range(
            archive.locator,
            start=data_start,
            end=data_end,
            expected_size=archive.size_bytes,
            expected_etag=archive.etag,
            expected_last_modified=archive.last_modified,
            approved_hosts=approved_hosts,
            maximum_bytes=self.limits.maximum_compressed_member_bytes,
            maximum_redirects=self.limits.maximum_redirects,
        )
        try:
            raw = compressed.content if method == 0 else zlib.decompress(compressed.content, -15)
        except zlib.error as exc:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_DECOMPRESSION_FAILED", "ZIP member decompression failed."
            ) from exc
        if len(raw) != member.uncompressed_size:
            raise RemoteZipAccessError("REMOTE_ZIP_SIZE_MISMATCH", "ZIP member size check failed.")
        if f"{binascii.crc32(raw) & 0xFFFFFFFF:08x}" != member.crc32:
            raise RemoteZipAccessError("REMOTE_ZIP_CRC_MISMATCH", "ZIP member CRC check failed.")
        if time.monotonic() - started > self.limits.maximum_wall_time_seconds:
            raise RemoteZipAccessError(
                "REMOTE_ZIP_WALL_TIME_LIMIT", "ZIP member extraction exceeded its wall-time limit."
            )
        descriptor = self.artifacts.put_bytes(
            workflow_id=workflow_id,
            content=raw,
            mime_type="application/octet-stream",
            artifact_type="selective_remote_zip_member",
            logical_name=logical_name,
            producer="selective-remote-zip",
            idempotency_key=identity,
            original_source=f"{archive.locator}#{member.member_name}",
        )
        evidence = [fixed.evidence, variable.evidence, compressed.evidence]
        source_request_count = sum(1 + item.redirect_count for item in evidence)
        return SelectiveZipMemberResult(
            member=member,
            artifact=ArtifactReference(
                artifact_id=descriptor.id,
                sha256=descriptor.sha256,
                artifact_type=descriptor.artifact_type,
            ),
            source_request_count=source_request_count,
            downloaded_bytes=sum(item.response_bytes for item in evidence),
            cache_hit=False,
            range_evidence=evidence,
        )


class PublicProviderHeavyFileFinding(ImmutableV2Contract):
    finding_code: Literal["PUBLIC_PROVIDER_HEAVY_FILE_REQUIRES_APPROVAL"] = (
        "PUBLIC_PROVIDER_HEAVY_FILE_REQUIRES_APPROVAL"
    )
    archive_locator: str = Field(pattern=r"^https://[^\s]+$")
    archive_size_bytes: int = Field(ge=1)
    required_members: list[str] = Field(min_length=1)
    required_member_reasons: dict[str, str]
    direct_file_evidence: str
    selective_access_evidence: str
    expected_staging_disk_bytes: int = Field(ge=1)
    replay_after_staging: str
    required_human_action: str
    safe_error_code: str


def persist_heavy_file_finding(
    artifacts: LocalArtifactStore,
    *,
    workflow_id: str,
    finding: PublicProviderHeavyFileFinding,
) -> ArtifactReference:
    payload = finding.model_dump_json(indent=2).encode()
    fingerprint = hashlib.sha256(payload).hexdigest()
    descriptor = artifacts.put_bytes(
        workflow_id=workflow_id,
        content=payload,
        mime_type="application/json",
        artifact_type="public_provider_heavy_file_finding",
        logical_name=f"public-provider-heavy-file-{fingerprint}.json",
        producer="selective-remote-zip",
        idempotency_key=fingerprint,
        original_source=finding.archive_locator,
    )
    return ArtifactReference(
        artifact_id=descriptor.id,
        sha256=descriptor.sha256,
        artifact_type=descriptor.artifact_type,
    )
