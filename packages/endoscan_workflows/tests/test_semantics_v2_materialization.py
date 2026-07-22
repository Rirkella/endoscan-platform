from __future__ import annotations

import sqlite3
from pathlib import Path

from endoscan_workflows.discovery_strategy import EvidenceRole
from endoscan_workflows.preapproval_providers import _create_schema
from endoscan_workflows.provider_result_materialization import compact_provider_dataset


def _raw_record(
    connection: sqlite3.Connection,
    record_id: str,
    kind: str,
    source_identifier: str,
    artifact_id: str,
) -> None:
    connection.execute(
        "INSERT INTO raw_records VALUES (?,?,?,?,?,?,?,?,?)",
        (
            record_id,
            "toxcast",
            "fixture-v1",
            "fixture-resource",
            kind,
            source_identifier,
            "{}",
            artifact_id,
            "a" * 64,
        ),
    )


def _assay(
    connection: sqlite3.Connection,
    *,
    ordinal: int,
    aeid: str,
    modality: str,
) -> None:
    raw_id = f"raw-assay-{ordinal}"
    _raw_record(connection, raw_id, "assay", f"AEID:{aeid}", f"art-assay-{ordinal}")
    connection.execute(
        "INSERT INTO assay_annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"annotation-{ordinal}",
            aeid,
            f"Fixture assay {aeid}",
            f"Component {aeid}",
            f"Endpoint {aeid}",
            "fixture",
            "assay_annotations",
            "protein",
            "receptor",
            "signal",
            "cell-based",
            "functional",
            "increase",
            "Homo sapiens",
            None,
            "CELL_A",
            "24",
            None,
            None,
            modality,
            "{}",
            "[]",
            raw_id,
        ),
    )
    connection.execute(
        "INSERT INTO activity_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"activity-{ordinal}",
            f"activity-source-{ordinal}",
            f"AEID:{aeid}",
            f"DTXSID{ordinal:07d}",
            None,
            "fixture target",
            modality,
            "active",
            "1",
            "unit",
            "{}",
            "{}",
            "[]",
            raw_id,
        ),
    )


def _large_toxcast_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        _create_schema(connection)
        _assay(connection, ordinal=1, aeid="101", modality="binding")
        _assay(connection, ordinal=2, aeid="102", modality="agonism")
        _assay(connection, ordinal=3, aeid="103", modality="antagonism")
        _raw_record(
            connection,
            "raw-identity-batch",
            "compound",
            "fixture-identity-batch",
            "art-identity-batch",
        )
        connection.executemany(
            "INSERT INTO compound_index VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    f"mapping-{index}",
                    f"DTXSID{index:07d}",
                    f"DTXSID{index:07d}",
                    None,
                    None,
                    None,
                    None,
                    "[]",
                    "source_backed",
                    "not_exposed_by_public_reference_table",
                    None,
                    "[]",
                    "raw-identity-batch",
                )
                for index in range(54_717)
            ),
        )


def test_large_toxcast_rows_compact_to_assay_sources_without_identity_candidates(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "large-toxcast.sqlite"
    _large_toxcast_sqlite(sqlite_path)

    by_modality = {
        modality: compact_provider_dataset(
            sqlite_path,
            provider="toxcast",
            evidence_role=EvidenceRole.ACTIVITY,
            task_modality=modality,
            release_id="invitrodb-v4.3-2025-08",
        )
        for modality in ("binding", "agonism", "antagonism")
    }

    assert {key: len(value) for key, value in by_modality.items()} == {
        "binding": 1,
        "agonism": 1,
        "antagonism": 1,
    }
    assert {item.source_identifier for values in by_modality.values() for item in values} == {
        "AEID:101",
        "AEID:102",
        "AEID:103",
    }
    assert all(
        item.coverage_summary["compound_activity_rows"] == 1
        for values in by_modality.values()
        for item in values
    )
    assert all(
        item.verified_metadata["row_level_data_embedded"] is False
        for values in by_modality.values()
        for item in values
    )


def test_identity_rows_become_one_mapping_set_with_disk_backed_counts(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "large-identity.sqlite"
    _large_toxcast_sqlite(sqlite_path)

    identity = compact_provider_dataset(
        sqlite_path,
        provider="pubchem-compound",
        evidence_role=EvidenceRole.IDENTITY,
        task_modality=None,
        release_id="pubchem-compound-reviewed-current",
    )

    assert len(identity) == 1
    assert identity[0].source_identifier == ("mapping-set:pubchem-compound-reviewed-current")
    assert identity[0].coverage_summary["mapping_rows"] == 54_717
    assert identity[0].verified_metadata["row_level_data_embedded"] is False
    assert identity[0].raw_artifact_ids == ["art-identity-batch"]
