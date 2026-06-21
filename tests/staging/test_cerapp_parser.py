"""CERAPP EXPERIMENTAL-call parser (predictions must not be consumed)."""

from __future__ import annotations

import cerapp  # noqa: E402 — resolved via conftest sys.path


def test_experimental_calls_mapped_unknowns_and_missing_dropped() -> None:
    rows = [
        {"casrn": "1-1-1", "er_experimental": "Active", "inchikey": "AAA"},
        {"casrn": "2-2-2", "er_experimental": "inactive"},
        {"casrn": "3-3-3", "er_experimental": "unknown"},  # ambiguous -> dropped
        {"casrn": "", "er_experimental": "active"},  # no casrn -> dropped
    ]
    out = cerapp.parse_cerapp_experimental(rows, activity_col="er_experimental")
    calls = {r["casrn"]: r["consensus_call"] for r in out}
    assert calls == {"1-1-1": "active", "2-2-2": "inactive"}
    assert all(r["label_provenance"] == "cerapp_experimental" for r in out)
    assert all(r["target"] == "ER" for r in out)
