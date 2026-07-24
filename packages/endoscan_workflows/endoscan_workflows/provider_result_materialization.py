"""Compact disk-backed provider results into semantics-v2 scientific source units.

Provider normalization intentionally retains row-level activity, identity, and
transcriptomic metadata in SQLite.  This module creates only the bounded source
units required by the workflow document; it never copies those scientific rows
into workflow JSON and never interprets endpoint-specific names.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .discovery_strategy import (
    CandidateStatus,
    EvidenceRole,
    HydrationCompleteness,
)

MAXIMUM_SOURCE_ARTIFACT_REFERENCES = 40
MAXIMUM_SUMMARY_VALUES = 40


@dataclass(frozen=True)
class CompactProviderSource:
    """One discoverable scientific source backed by a normalized provider artifact."""

    source_identifier: str
    evidence_role: EvidenceRole
    modality: str | None
    candidate_status: CandidateStatus
    verified_metadata: dict[str, Any]
    compound_index_available: bool
    label_or_activity_fields_available: bool
    organism: str | None
    experimental_context: dict[str, Any]
    completeness_status: HydrationCompleteness
    explicit_missing_fields: list[str] = field(default_factory=list)
    exclusion_reason: str | None = None
    raw_artifact_ids: list[str] = field(default_factory=list)
    coverage_summary: dict[str, Any] = field(default_factory=dict)
    relationship_summary: dict[str, Any] = field(default_factory=dict)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _count(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0] or 0) if row else 0


def _distinct_values(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(sql, parameters).fetchall()
        if row[0] not in (None, "")
    ][:MAXIMUM_SUMMARY_VALUES]


def _all_distinct_values(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[str]:
    """Return source-unit identifiers without sampling the candidate universe."""

    return [
        str(row[0])
        for row in connection.execute(sql, parameters).fetchall()
        if row[0] not in (None, "")
    ]


def _raw_artifact_ids(
    connection: sqlite3.Connection,
    selections: Iterable[tuple[str, tuple[Any, ...]]],
) -> tuple[list[str], int]:
    values: set[str] = set()
    total = 0
    for sql, parameters in selections:
        rows = connection.execute(sql, parameters).fetchall()
        total += len(rows)
        values.update(str(row[0]) for row in rows if row[0])
    ordered = sorted(values)
    return ordered[:MAXIMUM_SOURCE_ARTIFACT_REFERENCES], len(ordered)


def _without_prefix(value: str) -> str:
    return value.split(":", 1)[-1]


def _activity_source_identifier(provider: str, value: str) -> str:
    if ":" in value:
        return value
    prefix = "AEID" if provider == "toxcast" else "AID"
    return f"{prefix}:{value}"


def _activity_sources(
    connection: sqlite3.Connection,
    *,
    provider: str,
    task_modality: str | None,
    matched_source_identifiers: set[str] | None,
    modality_by_source: dict[str, str],
    provider_summary: dict[str, Any],
) -> list[CompactProviderSource]:
    tables = _tables(connection)
    source_modalities: set[tuple[str, str | None]] = set()
    if "assay_annotations" in tables:
        for source_identifier, modality in connection.execute(
            """
            SELECT DISTINCT aeid, modality FROM assay_annotations
            WHERE aeid IS NOT NULL AND TRIM(aeid) <> '' ORDER BY aeid, modality
            """
        ):
            normalized_identifier = _without_prefix(str(source_identifier))
            source_modality = str(modality) if modality else task_modality
            if matched_source_identifiers is not None and (
                normalized_identifier not in matched_source_identifiers
            ):
                continue
            if (
                matched_source_identifiers is None
                and task_modality is not None
                and source_modality is not None
                and source_modality != task_modality
            ):
                continue
            source_modalities.add((normalized_identifier, source_modality))
    if not source_modalities and "activity_records" in tables:
        for source_identifier, modality in connection.execute(
            """
            SELECT DISTINCT assay_identifier, modality FROM activity_records
            WHERE assay_identifier IS NOT NULL AND TRIM(assay_identifier) <> ''
            ORDER BY assay_identifier, modality
            """
        ):
            normalized_identifier = _without_prefix(str(source_identifier))
            source_modality = str(modality) if modality else task_modality
            if matched_source_identifiers is not None and (
                normalized_identifier not in matched_source_identifiers
            ):
                continue
            if task_modality and source_modality and source_modality != task_modality:
                continue
            source_modalities.add((normalized_identifier, source_modality))

    collapsed_source_modalities = {
        (
            normalized_identifier,
            modality_by_source.get(normalized_identifier) or task_modality or observed_modality,
        )
        for normalized_identifier, observed_modality in source_modalities
    }
    values: list[CompactProviderSource] = []
    for normalized_identifier, observed_modality in sorted(
        collapsed_source_modalities, key=lambda item: (item[0], item[1] or "")
    ):
        source_identifier = _activity_source_identifier(provider, normalized_identifier)
        effective_modality = observed_modality or None
        identifiers = (normalized_identifier, source_identifier)
        annotation_count = (
            _count(
                connection,
                "SELECT COUNT(*) FROM assay_annotations WHERE aeid IN (?,?)",
                identifiers,
            )
            if "assay_annotations" in tables
            else 0
        )
        activity_count = (
            _count(
                connection,
                "SELECT COUNT(*) FROM activity_records WHERE assay_identifier IN (?,?)",
                identifiers,
            )
            if "activity_records" in tables
            else 0
        )
        compound_count = (
            _count(
                connection,
                """
                SELECT COUNT(DISTINCT COALESCE(cid, compound_identifier))
                FROM activity_records WHERE assay_identifier IN (?,?)
                  AND COALESCE(cid, compound_identifier) IS NOT NULL
                """,
                identifiers,
            )
            if "activity_records" in tables
            else 0
        )
        labelled_activity_count = (
            _count(
                connection,
                """
                SELECT COUNT(*) FROM activity_records WHERE assay_identifier IN (?,?)
                  AND (activity_call IS NOT NULL OR activity_value IS NOT NULL)
                """,
                identifiers,
            )
            if "activity_records" in tables
            else 0
        )
        active_count = (
            _count(
                connection,
                """
                SELECT COUNT(*) FROM activity_records WHERE assay_identifier IN (?,?)
                  AND LOWER(TRIM(activity_call)) IN
                      ('active', 'agonist', 'antagonist', 'positive')
                """,
                identifiers,
            )
            if "activity_records" in tables
            else 0
        )
        inactive_count = (
            _count(
                connection,
                """
                SELECT COUNT(*) FROM activity_records WHERE assay_identifier IN (?,?)
                  AND LOWER(TRIM(activity_call)) IN
                      ('inactive', 'negative', 'no activity')
                """,
                identifiers,
            )
            if "activity_records" in tables
            else 0
        )
        summary_count = (
            _count(
                connection,
                "SELECT COUNT(*) FROM activity_summaries WHERE aeid IN (?,?)",
                identifiers,
            )
            if "activity_summaries" in tables
            else 0
        )
        relationship_count = (
            _count(
                connection,
                """
                SELECT COUNT(*) FROM assay_relationships
                WHERE parent_assay_identifier IN (?,?) OR child_assay_identifier IN (?,?)
                """,
                (*identifiers, *identifiers),
            )
            if "assay_relationships" in tables
            else 0
        )
        relationship_types = (
            _distinct_values(
                connection,
                """
                SELECT DISTINCT relationship_type FROM assay_relationships
                WHERE parent_assay_identifier IN (?,?) OR child_assay_identifier IN (?,?)
                ORDER BY relationship_type
                """,
                (*identifiers, *identifiers),
            )
            if "assay_relationships" in tables
            else []
        )
        annotation = None
        if annotation_count:
            annotation = connection.execute(
                """
                SELECT assay_name, component_name, endpoint_name, assay_source_name,
                       intended_target_type, intended_target_family, biological_process,
                       assay_design, assay_format, signal_direction, organism, tissue,
                       cell_model, timepoint_hours, viability_annotation, pubchem_aid,
                       source_fields_json
                FROM assay_annotations WHERE aeid IN (?,?)
                ORDER BY
                    (assay_name IS NOT NULL) DESC,
                    (intended_target_family IS NOT NULL) DESC,
                    annotation_id
                LIMIT 1
                """,
                identifiers,
            ).fetchone()
        annotation_fields = (
            {
                key: value
                for key, value in zip(
                    (
                        "assay_name",
                        "component_name",
                        "endpoint_name",
                        "assay_source_name",
                        "intended_target_type",
                        "intended_target_family",
                        "biological_process",
                        "assay_design",
                        "assay_format",
                        "signal_direction",
                        "organism",
                        "tissue",
                        "cell_model",
                        "timepoint_hours",
                        "viability_annotation",
                        "pubchem_aid",
                        "source_fields_json",
                    ),
                    annotation,
                    strict=True,
                )
                if value not in (None, "")
            }
            if annotation is not None
            else {}
        )
        source_fields: dict[str, Any] = {}
        source_fields_json = annotation_fields.pop("source_fields_json", None)
        if isinstance(source_fields_json, str):
            try:
                parsed_source_fields = json.loads(source_fields_json)
                if isinstance(parsed_source_fields, dict):
                    source_fields = parsed_source_fields
            except json.JSONDecodeError:
                source_fields = {}
        source_exclusion_reason = source_fields.get("exclusion_reason")
        outcome_representation_available = active_count + inactive_count > 0
        structural_validation_status = str(
            provider_summary.get("structural_validation_status", "incomplete")
        )
        exclusion_reason = (
            str(source_exclusion_reason)
            if source_exclusion_reason
            else (
                "No source-declared active/inactive outcome representation was retained."
                if activity_count and not outcome_representation_available
                else None
            )
        )
        scientifically_usable = (
            annotation_count > 0
            and compound_count > 0
            and outcome_representation_available
            and structural_validation_status == "valid"
            and exclusion_reason is None
        )
        raw_selections: list[tuple[str, tuple[Any, ...]]] = []
        if "assay_annotations" in tables:
            raw_selections.append(
                (
                    """
                    SELECT DISTINCT r.source_artifact_id FROM assay_annotations a
                    JOIN raw_records r ON r.record_id=a.raw_record_id
                    WHERE a.aeid IN (?,?) ORDER BY r.source_artifact_id
                    """,
                    identifiers,
                )
            )
        if "activity_records" in tables:
            raw_selections.append(
                (
                    """
                    SELECT DISTINCT r.source_artifact_id FROM activity_records a
                    JOIN raw_records r ON r.record_id=a.raw_record_id
                    WHERE a.assay_identifier IN (?,?) ORDER BY r.source_artifact_id
                    """,
                    identifiers,
                )
            )
        if "assay_relationships" in tables:
            raw_selections.append(
                (
                    """
                    SELECT DISTINCT r.source_artifact_id FROM assay_relationships a
                    JOIN raw_records r ON r.record_id=a.raw_record_id
                    WHERE a.parent_assay_identifier IN (?,?)
                       OR a.child_assay_identifier IN (?,?)
                    ORDER BY r.source_artifact_id
                    """,
                    (*identifiers, *identifiers),
                )
            )
        raw_ids, raw_artifact_count = _raw_artifact_ids(connection, raw_selections)
        missing = [] if annotation_count else ["verified_assay_annotation"]
        if compound_count == 0:
            missing.append("stable_compound_identifier")
        if not outcome_representation_available:
            missing.append("source_declared_active_inactive_outcome")
        if structural_validation_status != "valid":
            missing.append("mandatory_structural_validation")
        coverage = {
            "assay_annotation_rows": annotation_count,
            "compound_activity_rows": activity_count,
            "tested_compounds_with_identifiers": compound_count,
            "label_or_value_rows": labelled_activity_count,
            "active_rows": active_count,
            "inactive_rows": inactive_count,
            "ambiguous_rows": max(activity_count - active_count - inactive_count, 0),
            "outcome_representation_status": (
                "source_declared_active_inactive"
                if outcome_representation_available
                else "missing_active_inactive_representation"
            ),
            "activity_summary_rows": summary_count,
            "raw_artifact_count": raw_artifact_count,
            **provider_summary,
        }
        relationships = {
            "relationship_count": relationship_count,
            "relationship_types": relationship_types,
        }
        values.append(
            CompactProviderSource(
                source_identifier=source_identifier,
                evidence_role=EvidenceRole.ACTIVITY,
                modality=effective_modality,
                candidate_status=(
                    CandidateStatus.METADATA_CANDIDATE
                    if scientifically_usable
                    else CandidateStatus.EXCLUDED
                ),
                verified_metadata={
                    "scientific_source_unit": "assay_endpoint",
                    **annotation_fields,
                    "annotation_variant_count": annotation_count,
                    "activity_data_availability_inspected": bool(
                        provider_summary.get("activity_data_availability_inspected")
                    ),
                    "assay_relationships_inspected": bool(
                        provider_summary.get("assay_relationships_inspected")
                    ),
                    "structural_validation_status": structural_validation_status,
                    "target_relevance_status": source_fields.get(
                        "target_relevance_status", "unresolved"
                    ),
                    "activity_outcome_representation_available": (outcome_representation_available),
                    "row_level_data_embedded": False,
                },
                compound_index_available=compound_count > 0,
                label_or_activity_fields_available=outcome_representation_available,
                organism=(
                    str(annotation_fields["organism"]) if "organism" in annotation_fields else None
                ),
                experimental_context={
                    key: annotation_fields[key]
                    for key in ("tissue", "cell_model", "timepoint_hours")
                    if key in annotation_fields
                },
                completeness_status=(
                    HydrationCompleteness.COMPLETE
                    if scientifically_usable
                    else HydrationCompleteness.SCIENTIFICALLY_UNUSABLE
                ),
                explicit_missing_fields=missing,
                exclusion_reason=exclusion_reason,
                raw_artifact_ids=raw_ids,
                coverage_summary=coverage,
                relationship_summary=relationships,
            )
        )
    return values


def _identity_sources(
    connection: sqlite3.Connection,
    *,
    release_id: str,
) -> list[CompactProviderSource]:
    if "compound_index" not in _tables(connection):
        return []
    mapping_count = _count(connection, "SELECT COUNT(*) FROM compound_index")
    if not mapping_count:
        return []
    status_counts = {
        str(status): int(count)
        for status, count in connection.execute(
            """
            SELECT mapping_status, COUNT(*) FROM compound_index
            GROUP BY mapping_status ORDER BY mapping_status
            """
        )
    }
    identifier_counts = {
        field: _count(
            connection,
            f"SELECT COUNT(*) FROM compound_index WHERE {field} IS NOT NULL AND {field} <> ''",
        )
        for field in ("compound_identifier", "cid", "sid", "inchikey")
    }
    raw_ids, raw_artifact_count = _raw_artifact_ids(
        connection,
        [
            (
                """
                SELECT DISTINCT r.source_artifact_id FROM compound_index c
                JOIN raw_records r ON r.record_id=c.raw_record_id
                ORDER BY r.source_artifact_id
                """,
                (),
            )
        ],
    )
    return [
        CompactProviderSource(
            source_identifier=f"mapping-set:{release_id}",
            evidence_role=EvidenceRole.IDENTITY,
            modality=None,
            candidate_status=CandidateStatus.METADATA_CANDIDATE,
            verified_metadata={
                "scientific_source_unit": "reviewed_identity_mapping_set",
                "mapping_status_counts": status_counts,
                "identifier_type_counts": identifier_counts,
                "row_level_data_embedded": False,
            },
            compound_index_available=True,
            label_or_activity_fields_available=False,
            organism=None,
            experimental_context={},
            completeness_status=HydrationCompleteness.COMPLETE,
            raw_artifact_ids=raw_ids,
            coverage_summary={
                "mapping_rows": mapping_count,
                "raw_artifact_count": raw_artifact_count,
            },
        )
    ]


def _transcriptomic_sources(
    connection: sqlite3.Connection,
    *,
    provider: str,
) -> list[CompactProviderSource]:
    if "transcriptomic_records" not in _tables(connection):
        return []
    identifiers = _all_distinct_values(
        connection,
        """
        SELECT DISTINCT COALESCE(accession, source_identifier)
        FROM transcriptomic_records
        WHERE COALESCE(accession, source_identifier) IS NOT NULL
        ORDER BY COALESCE(accession, source_identifier)
        """,
    )
    values: list[CompactProviderSource] = []
    for identifier in identifiers:
        parameters = (identifier, identifier)
        record_count = _count(
            connection,
            """
            SELECT COUNT(*) FROM transcriptomic_records
            WHERE accession=? OR source_identifier=?
            """,
            parameters,
        )
        outcome_counts = {
            str(outcome): int(count)
            for outcome, count in connection.execute(
                """
                SELECT outcome, COUNT(*) FROM transcriptomic_records
                WHERE accession=? OR source_identifier=?
                GROUP BY outcome ORDER BY outcome
                """,
                parameters,
            )
        }
        organisms = _distinct_values(
            connection,
            """
            SELECT DISTINCT organism FROM transcriptomic_records
            WHERE accession=? OR source_identifier=? ORDER BY organism
            """,
            parameters,
        )
        models = _distinct_values(
            connection,
            """
            SELECT DISTINCT biological_model FROM transcriptomic_records
            WHERE accession=? OR source_identifier=? ORDER BY biological_model
            """,
            parameters,
        )
        processing_levels = _distinct_values(
            connection,
            """
            SELECT DISTINCT processing_level FROM transcriptomic_records
            WHERE accession=? OR source_identifier=? ORDER BY processing_level
            """,
            parameters,
        )
        doses = _distinct_values(
            connection,
            """
            SELECT DISTINCT dose FROM transcriptomic_records
            WHERE accession=? OR source_identifier=? ORDER BY dose
            """,
            parameters,
        )
        exposure_times = _distinct_values(
            connection,
            """
            SELECT DISTINCT exposure_time FROM transcriptomic_records
            WHERE accession=? OR source_identifier=? ORDER BY exposure_time
            """,
            parameters,
        )
        stable_compound_ids = _all_distinct_values(
            connection,
            """
            SELECT DISTINCT compound_identifier FROM transcriptomic_records
            WHERE (accession=? OR source_identifier=?)
              AND compound_identifier IS NOT NULL
              AND TRIM(compound_identifier) <> ''
            ORDER BY compound_identifier
            """,
            parameters,
        )
        compound_count = _count(
            connection,
            """
            SELECT COUNT(DISTINCT compound_identifier) FROM transcriptomic_records
            WHERE (accession=? OR source_identifier=?) AND compound_identifier IS NOT NULL
            """,
            parameters,
        )
        verified_perturbation_count = _count(
            connection,
            """
            SELECT COUNT(*) FROM transcriptomic_records
            WHERE (accession=? OR source_identifier=?) AND perturbation_verified=1
            """,
            parameters,
        )
        matrix_locator_count = _count(
            connection,
            """
            SELECT COUNT(DISTINCT matrix_locator) FROM transcriptomic_records
            WHERE (accession=? OR source_identifier=?) AND matrix_locator IS NOT NULL
            """,
            parameters,
        )
        raw_ids, raw_artifact_count = _raw_artifact_ids(
            connection,
            [
                (
                    """
                    SELECT DISTINCT r.source_artifact_id FROM transcriptomic_records t
                    JOIN raw_records r ON r.record_id=t.raw_record_id
                    WHERE t.accession=? OR t.source_identifier=?
                    ORDER BY r.source_artifact_id
                    """,
                    parameters,
                )
            ],
        )
        missing = []
        if not compound_count:
            missing.append("stable_compound_identifier")
        if not matrix_locator_count:
            missing.append("processed_matrix_locator")
        if not models:
            missing.append("cell_or_tissue_model")
        if not doses:
            missing.append("dose")
        if not exposure_times:
            missing.append("exposure_duration")
        if not verified_perturbation_count:
            missing.append("verified_chemical_perturbation")
        source_class = (
            "geo_supplemental"
            if provider == "ncbi-geo"
            else "registered_perturbational_transcriptomics"
        )
        treatment_design_verified = outcome_counts.get("verified_compound_perturbation", 0) > 0
        joinability_eligible = all(
            (
                bool(stable_compound_ids),
                bool(matrix_locator_count),
                bool(models),
                bool(doses),
                bool(exposure_times),
                bool(verified_perturbation_count),
                treatment_design_verified if provider == "ncbi-geo" else True,
            )
        )
        exclusion_reason = None
        if not joinability_eligible:
            exclusion_reason = (
                "Supplemental transcriptomic evidence is not assembly-eligible: "
                + ", ".join(missing or ["treatment/control design not verified"])
                + "."
            )
        values.append(
            CompactProviderSource(
                source_identifier=identifier,
                evidence_role=EvidenceRole.TRANSCRIPTOMIC,
                modality=None,
                candidate_status=CandidateStatus.METADATA_CANDIDATE,
                verified_metadata={
                    "scientific_source_unit": "transcriptomic_study",
                    "hydration_classification_counts": outcome_counts,
                    "organisms": organisms,
                    "biological_models": models,
                    "processing_levels": processing_levels,
                    "processed_matrix_available": matrix_locator_count > 0,
                    "transcriptomic_source_class": source_class,
                    "stable_compound_ids": stable_compound_ids,
                    "stable_identity_bridge_available": bool(stable_compound_ids),
                    "treatment_control_design_verified": treatment_design_verified,
                    "expression_extraction_path": (
                        "approved_processed_matrix_locator"
                        if matrix_locator_count
                        else "unavailable"
                    ),
                    "joinability_eligible": joinability_eligible,
                    "row_level_data_embedded": False,
                },
                compound_index_available=compound_count > 0,
                label_or_activity_fields_available=False,
                organism=organisms[0] if len(organisms) == 1 else None,
                experimental_context={
                    "cell": models,
                    "dose": doses,
                    "time": exposure_times,
                    "biological_models": models,
                },
                completeness_status=(
                    HydrationCompleteness.COMPLETE
                    if joinability_eligible
                    else HydrationCompleteness.SCIENTIFICALLY_UNUSABLE
                ),
                explicit_missing_fields=missing,
                exclusion_reason=exclusion_reason,
                raw_artifact_ids=raw_ids,
                coverage_summary={
                    "metadata_rows": record_count,
                    "profile_count": record_count,
                    "compound_identifier_count": compound_count,
                    "processed_matrix_locator_count": matrix_locator_count,
                    "verified_perturbation_count": verified_perturbation_count,
                    "raw_artifact_count": raw_artifact_count,
                },
            )
        )
    return values


def _supporting_sources(connection: sqlite3.Connection) -> list[CompactProviderSource]:
    tables = _tables(connection)
    if "raw_records" not in tables:
        return []
    identifiers = _all_distinct_values(
        connection,
        """
        SELECT DISTINCT source_identifier FROM raw_records
        WHERE record_kind IN ('publication','dataset_link')
          AND source_identifier IS NOT NULL
        ORDER BY source_identifier
        """,
    )
    values: list[CompactProviderSource] = []
    for identifier in identifiers:
        record_count = _count(
            connection,
            """
            SELECT COUNT(*) FROM raw_records
            WHERE source_identifier=? AND record_kind IN ('publication','dataset_link')
            """,
            (identifier,),
        )
        link_count = (
            _count(
                connection,
                "SELECT COUNT(*) FROM publication_dataset_links WHERE source_identifier=?",
                (identifier,),
            )
            if "publication_dataset_links" in tables
            else 0
        )
        raw_ids = _distinct_values(
            connection,
            """
            SELECT DISTINCT source_artifact_id FROM raw_records
            WHERE source_identifier=? AND record_kind IN ('publication','dataset_link')
            ORDER BY source_artifact_id
            """,
            (identifier,),
        )[:MAXIMUM_SOURCE_ARTIFACT_REFERENCES]
        values.append(
            CompactProviderSource(
                source_identifier=identifier,
                evidence_role=EvidenceRole.SUPPORTING_METADATA,
                modality=None,
                candidate_status=CandidateStatus.METADATA_CANDIDATE,
                verified_metadata={
                    "scientific_source_unit": "reviewed_supporting_source",
                    "metadata_record_count": record_count,
                    "publication_dataset_link_count": link_count,
                    "row_level_data_embedded": False,
                },
                compound_index_available=False,
                label_or_activity_fields_available=False,
                organism=None,
                experimental_context={},
                completeness_status=HydrationCompleteness.COMPLETE,
                raw_artifact_ids=raw_ids,
                coverage_summary={"metadata_rows": record_count},
                relationship_summary={"publication_dataset_links": link_count},
            )
        )
    return values


def compact_provider_dataset(
    sqlite_path: Path,
    *,
    provider: str,
    evidence_role: EvidenceRole,
    task_modality: str | None,
    release_id: str,
    matched_source_identifiers: set[str] | None = None,
    modality_by_source: dict[str, str] | None = None,
    provider_summary: dict[str, Any] | None = None,
) -> list[CompactProviderSource]:
    """Return deterministic compact units while leaving all detailed rows on disk."""

    with sqlite3.connect(sqlite_path) as connection:
        if evidence_role is EvidenceRole.ACTIVITY:
            return _activity_sources(
                connection,
                provider=provider,
                task_modality=task_modality,
                matched_source_identifiers=matched_source_identifiers,
                modality_by_source=modality_by_source or {},
                provider_summary=provider_summary or {},
            )
        if evidence_role is EvidenceRole.IDENTITY:
            return _identity_sources(connection, release_id=release_id)
        if evidence_role is EvidenceRole.TRANSCRIPTOMIC:
            return _transcriptomic_sources(connection, provider=provider)
        if evidence_role is EvidenceRole.SUPPORTING_METADATA:
            return _supporting_sources(connection)
    raise ValueError(f"Unsupported provider evidence role: {evidence_role.value}")
