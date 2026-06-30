"""Re-registration: overwrite a non-frozen endpoint; refuse frozen ones; keep the gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from endoscan_core.registry import (
    EndpointEntry,
    EndpointStatus,
    FrozenEndpointError,
    RegistryValidationError,
    get_endpoint,
    register_endpoint,
    register_or_update_endpoint,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_reregister_overwrites_nonfrozen_entry(tmp_registry: Path, fixture_entry: EndpointEntry):
    register_endpoint(fixture_entry, repo_root=tmp_registry)

    # Re-promote: a NEW entry for the SAME id (updated status + version + a later timestamp).
    updated_in = fixture_entry.model_copy(
        update={
            "status": EndpointStatus.experimental,
            "version": "0.2.0",
            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    result = register_or_update_endpoint(updated_in, repo_root=tmp_registry)

    stored = get_endpoint(fixture_entry.endpoint_id, repo_root=tmp_registry)
    assert stored.status is EndpointStatus.experimental and stored.version == "0.2.0"
    # Audit trail: the ORIGINAL created_at is preserved; updated_at records the re-promote.
    assert stored.created_at == fixture_entry.created_at  # not silently mutated
    assert stored.updated_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert result.updated_at == stored.updated_at
    # Exactly one entry remains for the id (overwrite in place, not a duplicate append).
    index = json.loads((tmp_registry / "registry/models/endpoints.json").read_text())
    assert [e["endpoint_id"] for e in index["endpoints"]].count(fixture_entry.endpoint_id) == 1


def test_reregister_enforces_all_artifacts_present(
    tmp_registry: Path, fixture_entry: EndpointEntry
):
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    # Re-register with a metrics path that does not exist on disk -> refused (same gate).
    broken = fixture_entry.model_copy(update={"metrics_path": "models/DEMO_ER/missing.json"})
    with pytest.raises(RegistryValidationError, match="missing artifact"):
        register_or_update_endpoint(broken, repo_root=tmp_registry)
    # The original entry is left intact (no partial overwrite).
    assert get_endpoint(fixture_entry.endpoint_id, repo_root=tmp_registry).metrics_path.endswith(
        "metrics.json"
    )


def test_reregister_refuses_frozen_endpoint(tmp_registry: Path, fixture_entry: EndpointEntry):
    frozen = fixture_entry.model_copy(update={"frozen": True})
    register_endpoint(frozen, repo_root=tmp_registry)
    with pytest.raises(FrozenEndpointError, match="frozen"):
        register_or_update_endpoint(
            fixture_entry.model_copy(update={"version": "9.9.9"}), repo_root=tmp_registry
        )
    # Unchanged — a frozen endpoint is immutable through this path.
    assert get_endpoint(fixture_entry.endpoint_id, repo_root=tmp_registry).version == "0.1.0"


def test_committed_er_is_frozen_and_cannot_be_overwritten(tmp_registry: Path):
    # The committed registry marks ER frozen; copying that entry into a throwaway index and
    # attempting to overwrite it is refused (ER stays immutable — never edited manually).
    real = json.loads((REPO_ROOT / "registry/models/endpoints.json").read_text())
    er = next(e for e in real["endpoints"] if e["endpoint_id"] == "ER")
    assert er["frozen"] is True  # ER is marked frozen in the real registry

    idx_path = tmp_registry / "registry/models/endpoints.json"
    idx_path.write_text(json.dumps({"endpoints": [er]}, indent=2), encoding="utf-8")
    er_entry = EndpointEntry.model_validate(er)
    with pytest.raises(FrozenEndpointError):
        register_or_update_endpoint(
            er_entry.model_copy(update={"status": EndpointStatus.validated_mvp}),
            repo_root=tmp_registry,
        )


def test_first_registration_matches_register_endpoint(
    tmp_registry: Path, fixture_entry: EndpointEntry
):
    # A NEW id through the re-register path behaves exactly like register_endpoint (append).
    result = register_or_update_endpoint(fixture_entry, repo_root=tmp_registry)
    assert result.updated_at is None  # not a re-registration -> no update stamp
    assert get_endpoint(fixture_entry.endpoint_id, repo_root=tmp_registry).version == "0.1.0"
    # register_endpoint's duplicate-id behavior is UNCHANGED (still raises on a second append).
    with pytest.raises(RegistryValidationError, match="already registered"):
        register_endpoint(fixture_entry, repo_root=tmp_registry)
