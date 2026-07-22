from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from endoscan_workflows.toxcast_public_activity import (
    ArchiveProbe,
    ArchiveTransportAttempt,
    ToxCastActivityNormalizer,
    ToxCastArchiveCache,
    ToxCastArchiveProcessor,
    ToxCastCoverageQuery,
    ToxCastCoverageService,
    ToxCastMemberRole,
    ToxCastRoleRule,
)


class FixtureArchiveTransport:
    def __init__(self, source: Path, *, fail_first: bool = False) -> None:
        self.source = source
        self.fail_first = fail_first
        self.probes = 0
        self.downloads = 0

    def probe(self, locator: str, *, expected_size: int) -> ArchiveProbe:
        self.probes += 1
        assert expected_size == self.source.stat().st_size
        return ArchiveProbe(
            final_locator=locator,
            http_status=206,
            content_length_bytes=expected_size,
            content_type="application/zip",
            etag='"fixture-etag"',
            last_modified="Tue, 21 Jul 2026 00:00:00 GMT",
            final_host="clowder.edap-cluster.com",
            redirect_count=0,
        )

    def stream_download(
        self,
        locator: str,
        destination: Path,
        *,
        expected_size: int,
        expected_identity: ArchiveProbe,
        attempt_number: int,
    ) -> tuple[ArchiveTransportAttempt, str]:
        del locator, expected_identity
        self.downloads += 1
        if self.fail_first and attempt_number == 1:
            destination.write_bytes(b"partial")
            return (
                ArchiveTransportAttempt(
                    attempt_number=attempt_number,
                    outcome="failed",
                    bytes_received=7,
                    duration_ms=1,
                    exception_class="ConnectionError",
                    safe_message="Reviewed fixture interruption.",
                    retryable=True,
                ),
                "",
            )
        content = self.source.read_bytes()
        assert len(content) == expected_size
        destination.write_bytes(content)
        return (
            ArchiveTransportAttempt(
                attempt_number=attempt_number,
                outcome="completed",
                bytes_received=len(content),
                duration_ms=1,
                http_status=200,
                final_allowlisted_host="clowder.edap-cluster.com",
            ),
            hashlib.sha256(content).hexdigest(),
        )


def _csv(rows: list[dict[str, object]]) -> str:
    columns = list(rows[0])
    output = []
    output.append(",".join(columns))
    for row in rows:
        output.append(",".join(str(row.get(column, "")) for column in columns))
    return "\n".join(output) + "\n"


def _archive(path: Path) -> Path:
    documentation = """
    invitroDB v4.3 public schema documentation.
    chemicals.csv is the complete tested chemical catalogue.
    mc4_model_fits.csv contains multi-concentration model fit records.
    mc5_winning_fits_flags.csv contains multi-concentration hit call and flag records.
    sc1_sc2.csv contains single-concentration hit call records.
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", documentation)
        archive.writestr(
            "chemicals.csv",
            _csv(
                [
                    {
                        "dtxsid": "DTXSID1",
                        "spid": "SPID1",
                        "casrn": "50-00-0",
                        "chemical_name": "Example one",
                        "cid": "1",
                        "inchikey": "AAAAAAAAAAAAAA-BBBBBBBBBB-C",
                        "qc_status": "reviewed",
                    },
                    {
                        "dtxsid": "DTXSID2",
                        "spid": "SPID2",
                        "casrn": "60-00-0",
                        "chemical_name": "Example two",
                        "cid": "2",
                        "inchikey": "CCCCCCCCCCCCCC-DDDDDDDDDD-E",
                        "qc_status": "flagged",
                    },
                ]
            ),
        )
        archive.writestr(
            "mc4_model_fits.csv",
            _csv(
                [
                    {
                        "dtxsid": "DTXSID1",
                        "spid": "SPID1",
                        "aeid": "101",
                        "modl": "hill",
                        "resp_max": "1.2",
                        "modl_acc": "0.3",
                    }
                ]
            ),
        )
        archive.writestr(
            "mc5_winning_fits_flags.csv",
            _csv(
                [
                    {
                        "dtxsid": "DTXSID1",
                        "spid": "SPID1",
                        "aeid": "101",
                        # invitroDB v4 uses a continuous source hit call. Preserve it
                        # verbatim instead of inventing a binary activity threshold.
                        "hitc": "0.73",
                        "resp_max": "1.2",
                        "modl_acc": "0.3",
                        "flag": "",
                    },
                    {
                        "dtxsid": "DTXSID2",
                        "spid": "SPID2",
                        "aeid": "101",
                        "hitc": "0",
                        "resp_max": "0.1",
                        "modl_acc": "10",
                        "flag": "caution",
                    },
                ]
            ),
        )
        archive.writestr(
            "sc1_sc2.csv",
            _csv(
                [
                    {
                        "dtxsid": "DTXSID1",
                        "spid": "SPID1",
                        "aeid": "101",
                        "hitc": "1",
                        "conc": "10",
                        "resp": "2.0",
                        "flag": "",
                    }
                ]
            ),
        )
        archive.writestr("unrelated.csv", "value\n1\n")
    return path


def _rules() -> list[ToxCastRoleRule]:
    return [
        ToxCastRoleRule(
            role=ToxCastMemberRole.TESTED_CHEMICALS,
            filename_pattern=r"chemicals\.csv$",
            required_header_groups=[["dtxsid"], ["spid"], ["chemical_name"]],
            documentation_terms=["complete tested chemical catalogue"],
        ),
        ToxCastRoleRule(
            role=ToxCastMemberRole.MULTI_CONCENTRATION_MODEL_FITS,
            filename_pattern=r"mc4.*\.csv$",
            required_header_groups=[["aeid"], ["dtxsid"], ["modl"]],
            documentation_terms=["multi-concentration model fit"],
        ),
        ToxCastRoleRule(
            role=ToxCastMemberRole.MULTI_CONCENTRATION_HIT_CALLS,
            filename_pattern=r"mc5.*\.csv$",
            required_header_groups=[["aeid"], ["dtxsid"], ["hitc"]],
            documentation_terms=["hit call", "flag"],
        ),
        ToxCastRoleRule(
            role=ToxCastMemberRole.SINGLE_CONCENTRATION_ACTIVITY,
            filename_pattern=r"sc1.*\.csv$",
            required_header_groups=[["aeid"], ["dtxsid"], ["hitc"]],
            documentation_terms=["single-concentration hit call"],
        ),
    ]


def test_archive_stages_once_verifies_roles_normalizes_and_replays(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    transport = FixtureArchiveTransport(source)
    cache = ToxCastArchiveCache(tmp_path / "provider-cache", transport)

    stage = cache.stage(expected_size=source.stat().st_size)
    assert not stage.cache_hit
    assert stage.network_request_count == 2
    assert stage.downloaded_bytes == source.stat().st_size
    replay = cache.stage(expected_size=source.stat().st_size)
    assert replay.cache_hit
    assert replay.network_request_count == 0
    assert replay.archive.sha256 == stage.archive.sha256
    assert transport.probes == 1
    assert transport.downloads == 1

    processor = ToxCastArchiveProcessor(cache)
    inventory = processor.enumerate(stage)
    assert inventory.exact_member_count == 6
    verified = processor.verify_and_extract_roles(stage, inventory, role_rules=_rules())
    assert verified.all_required_roles_verified
    assert not verified.ambiguity_findings
    assert sum(item.extracted_file is not None for item in verified.members) == 5

    normalization_temp = tmp_path / "normalization-temp"
    normalizer = ToxCastActivityNormalizer(cache, temporary_root=normalization_temp)
    normalized = normalizer.normalize(verified)
    assert normalized.tested_chemical_rows == 2
    assert normalized.multi_concentration_model_fit_rows == 1
    assert normalized.multi_concentration_hit_call_rows == 2
    assert normalized.single_concentration_rows == 1
    assert {
        "idx_mc_fit_aeid_dtxsid",
        "idx_mc_hit_aeid_dtxsid",
        "idx_sc_aeid_dtxsid",
    } <= set(normalized.index_names)
    normalized_replay = normalizer.normalize(verified)
    assert not normalized_replay.normalization_ran
    assert normalized_replay.bundle_fingerprint == normalized.bundle_fingerprint
    assert list(normalization_temp.glob("toxcast-activity-*.sqlite")) == []

    coverage = ToxCastCoverageService(cache).compute(
        normalized,
        ToxCastCoverageQuery(
            biological_target="example receptor",
            matched_aeids=["AEID:101"],
            modality_by_aeid={"AEID:101": "binding"},
            upstream_identity_set=["DTXSID1"],
        ),
    )
    assert coverage.unique_tested_compounds == 2
    assert coverage.multi_concentration_rows == 2
    assert coverage.single_concentration_rows == 1
    assert coverage.multi_hit_call_distribution == {"0": 1, "0.73": 1}
    assert coverage.upstream_overlap_count == 1


def test_single_transient_failure_retries_once_and_removes_partial(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    transport = FixtureArchiveTransport(source, fail_first=True)
    cache = ToxCastArchiveCache(tmp_path / "provider-cache", transport)
    stage = cache.stage(expected_size=source.stat().st_size, maximum_full_download_retries=1)
    assert len(stage.transport_attempts) == 2
    assert stage.transport_attempts[0].retryable
    assert stage.transport_attempts[1].outcome == "completed"
    assert not (cache.release_root / "INVITRODB_SUMMARY.zip.partial").exists()


def test_exact_complete_partial_is_validated_and_recovered_without_network(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    transport = FixtureArchiveTransport(source)
    cache = ToxCastArchiveCache(tmp_path / "provider-cache", transport)
    partial = cache.release_root / "INVITRODB_SUMMARY.zip.partial"
    partial.write_bytes(source.read_bytes())

    recovered = cache.stage(expected_size=source.stat().st_size)

    assert recovered.transport_attempts[0].outcome == ("completed_recovered_after_operator_timeout")
    assert recovered.archive.size_bytes == source.stat().st_size
    assert recovered.network_request_count == 2
    assert recovered.temporary_file_removed
    assert transport.probes == 0
    assert transport.downloads == 0
    replay = cache.stage(expected_size=source.stat().st_size)
    assert replay.cache_hit
    assert replay.network_request_count == 0


def test_role_ambiguity_stops_before_normalization(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    transport = FixtureArchiveTransport(source)
    cache = ToxCastArchiveCache(tmp_path / "provider-cache", transport)
    stage = cache.stage(expected_size=source.stat().st_size)
    processor = ToxCastArchiveProcessor(cache)
    inventory = processor.enumerate(stage)
    ambiguous = processor.verify_and_extract_roles(stage, inventory, role_rules=[])
    assert not ambiguous.all_required_roles_verified
    with pytest.raises(ValueError, match="ambiguous"):
        ToxCastActivityNormalizer(cache).normalize(ambiguous)


def test_cached_archive_tamper_forces_revalidation(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    transport = FixtureArchiveTransport(source)
    cache = ToxCastArchiveCache(tmp_path / "provider-cache", transport)
    stage = cache.stage(expected_size=source.stat().st_size)
    archive_path = cache.root / stage.archive.cache_relative_path
    archive_path.chmod(0o600)
    archive_path.write_bytes(b"tampered")
    repaired = cache.stage(expected_size=source.stat().st_size)
    assert not repaired.cache_hit
    assert repaired.archive.sha256 == stage.archive.sha256
    assert transport.downloads == 2


def test_fixture_archive_contains_no_large_or_unreviewed_extraction(tmp_path: Path) -> None:
    source = _archive(tmp_path / "fixture.zip")
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        assert "unrelated.csv" in names
        assert all(not Path(name).is_absolute() for name in names)
        assert json.loads(json.dumps(names)) == names
