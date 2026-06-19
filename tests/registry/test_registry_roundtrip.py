"""Round-trip: register → get → list → update_status, plus load_model + templates."""

from __future__ import annotations

from pathlib import Path

from endoscan_core.registry import (
    EndpointEntry,
    EndpointStatus,
    get_endpoint,
    list_endpoints,
    load_model,
    register_endpoint,
    render_template,
    template_placeholders,
    update_status,
)


def test_register_get_list_update_roundtrip(
    tmp_registry: Path, fixture_entry: EndpointEntry
) -> None:
    assert list_endpoints(repo_root=tmp_registry) == []

    registered = register_endpoint(fixture_entry, repo_root=tmp_registry)
    assert registered.endpoint_id == "DEMO_ER"

    fetched = get_endpoint("DEMO_ER", repo_root=tmp_registry)
    assert fetched == fixture_entry

    listed = list_endpoints(repo_root=tmp_registry)
    assert [entry.endpoint_id for entry in listed] == ["DEMO_ER"]

    updated = update_status("DEMO_ER", EndpointStatus.dataset_ready, repo_root=tmp_registry)
    assert updated.status is EndpointStatus.dataset_ready
    assert get_endpoint("DEMO_ER", repo_root=tmp_registry).status is EndpointStatus.dataset_ready


def test_update_status_accepts_string_value(
    tmp_registry: Path, fixture_entry: EndpointEntry
) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    updated = update_status("DEMO_ER", "under_review", repo_root=tmp_registry)
    assert updated.status is EndpointStatus.under_review


def test_load_model_returns_stub(tmp_registry: Path, fixture_entry: EndpointEntry) -> None:
    register_endpoint(fixture_entry, repo_root=tmp_registry)
    model = load_model("DEMO_ER", repo_root=tmp_registry)
    assert model["kind"] == "endoscan-demo-stub"
    assert model["n_features"] == 3


def test_templates_render_with_placeholders() -> None:
    tokens = template_placeholders("model_card.md")
    assert tokens  # the template actually uses placeholders
    values = {token: f"<{token}>" for token in tokens}
    rendered = render_template("model_card.md", values)
    assert "{{" not in rendered
    assert "Limitations" in rendered
