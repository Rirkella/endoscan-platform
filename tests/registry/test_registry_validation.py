"""Validation: bad input_type, bad status, and the validated_mvp artifact gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from endoscan_core.registry import (
    EndpointEntry,
    EndpointNotFoundError,
    EndpointStatus,
    RegistryValidationError,
    get_endpoint,
    register_endpoint,
    update_status,
)


def _entry_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "endpoint_id": "DEMO_ER",
        "biological_target": "Estrogen Receptor (demo fixture)",
        "input_type": "transcriptomics",
        "model_path": "models/DEMO_ER/model.pkl",
        "feature_schema_path": "models/DEMO_ER/feature_schema.json",
        "metrics_path": "models/DEMO_ER/metrics.json",
        "explainer_path": None,
        "model_card_path": "models/DEMO_ER/model_card.md",
        "dataset_card_path": "models/DEMO_ER/dataset_card.md",
        "status": "candidate",
        "version": "0.1.0",
        "created_at": "2026-06-19T00:00:00Z",
        "source_refs": ["demo:fixture-er-v0"],
    }
    base.update(overrides)
    return base


def test_rejects_non_transcriptomics_input_type() -> None:
    with pytest.raises(ValidationError):
        EndpointEntry(**_entry_kwargs(input_type="smiles"))


def test_rejects_invalid_status_at_construction() -> None:
    with pytest.raises(ValidationError):
        EndpointEntry(**_entry_kwargs(status="approved"))


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EndpointEntry(**_entry_kwargs(smiles="CCO"))


def test_update_status_rejects_invalid_status(
    tmp_registry: Path, fixture_entry: EndpointEntry
) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    with pytest.raises(ValueError):
        update_status("DEMO_ER", "approved", repo_root=tmp_registry)


def test_validated_mvp_blocked_when_artifact_missing(
    tmp_registry: Path, fixture_entry: EndpointEntry
) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    (tmp_registry / fixture_entry.metrics_path).unlink()
    with pytest.raises(RegistryValidationError):
        update_status("DEMO_ER", EndpointStatus.validated_mvp, repo_root=tmp_registry)
    # The blocked promotion left the status unchanged.
    assert get_endpoint("DEMO_ER", repo_root=tmp_registry).status is EndpointStatus.candidate


def test_validated_mvp_succeeds_when_all_artifacts_exist(
    tmp_registry: Path, fixture_entry: EndpointEntry
) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    updated = update_status("DEMO_ER", EndpointStatus.validated_mvp, repo_root=tmp_registry)
    assert updated.status is EndpointStatus.validated_mvp


def test_register_as_validated_mvp_requires_artifacts(tmp_registry: Path) -> None:
    entry = EndpointEntry(
        **_entry_kwargs(status="validated_mvp", model_path="models/DEMO_ER/missing.pkl")
    )
    with pytest.raises(RegistryValidationError):
        register_endpoint(entry, repo_root=tmp_registry)


def test_unknown_endpoint_raises(tmp_registry: Path) -> None:
    with pytest.raises(EndpointNotFoundError):
        get_endpoint("NOPE", repo_root=tmp_registry)


def test_duplicate_registration_raises(tmp_registry: Path, fixture_entry: EndpointEntry) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    with pytest.raises(RegistryValidationError):
        register_endpoint(fixture_entry, repo_root=tmp_registry)
