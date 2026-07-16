"""Full-signature preranked pathway analysis and honest input-type behavior."""

from __future__ import annotations

import pytest

from endoscan_api.biological_response import run_preranked_enrichment
from endoscan_api.pathways import Pathway
from endoscan_api.reactome_store import ReactomeData


def _ranked_fixture():
    signature = {f"G{index:02d}": float(21 - index) for index in range(1, 21)}
    signature.update({f"N{index:02d}": float(-index) for index in range(1, 21)})
    pathways = [
        Pathway("UP", "Increased process", frozenset(f"G{index:02d}" for index in range(1, 9))),
        Pathway("DOWN", "Decreased process", frozenset(f"N{index:02d}" for index in range(13, 21))),
        Pathway(
            "BACKGROUND",
            "Background process",
            frozenset(
                [
                    *(f"G{index:02d}" for index in range(1, 21)),
                    *(f"N{index:02d}" for index in range(1, 21)),
                ]
            ),
        ),
    ]
    return signature, pathways


def test_preranked_response_separates_positive_and_negative_directions() -> None:
    signature, pathways = _ranked_fixture()
    result = run_preranked_enrichment(signature, pathways)
    assert result.increased[0].pathway_id == "UP"
    assert result.increased[0].enrichment_statistic > 0
    assert result.decreased[0].pathway_id == "DOWN"
    assert result.decreased[0].enrichment_statistic < 0
    assert result.increased[0].leading_edge_genes[0] == "G01"
    assert result.decreased[0].leading_edge_genes[0] == "N20"
    assert result.pathways_tested == 2  # whole-universe set is intentionally not competitive


def test_preranked_response_empty_when_no_gene_set_meets_size_bounds() -> None:
    signature = {f"G{index}": float(index) for index in range(10)}
    pathways = [Pathway("SMALL", "Small", frozenset({"G1", "G2"}))]
    result = run_preranked_enrichment(signature, pathways)
    assert result.increased == [] and result.decreased == []
    assert result.pathways_tested == 0


@pytest.mark.parametrize(
    "input_value_type",
    ["differential_zscore", "log2_fold_change", "ranked_statistic"],
)
def test_supported_signed_input_types_run_and_are_recorded(
    client, monkeypatch, input_value_type: str
) -> None:
    signature, pathways = _ranked_fixture()
    monkeypatch.setattr(
        "endoscan_api.routes.interpret.load_reactome",
        lambda _repo_root: ReactomeData(
            pathways=pathways,
            provenance={"reactome_version": "test-version", "license": "CC0"},
        ),
    )
    response = client.post(
        "/interpret/biological-response",
        json={"signature": signature, "input_value_type": input_value_type},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["increased_pathways"][0]["pathway_id"] == "UP"
    assert body["decreased_pathways"][0]["pathway_id"] == "DOWN"
    assert body["method_block"]["input_value_type"] == input_value_type
    assert body["method_block"]["reactome"]["reactome_version"] == "test-version"


def test_raw_expression_returns_honest_unavailable_state(client) -> None:
    response = client.post(
        "/interpret/biological-response",
        json={"signature": {"A": 1.0, "B": 2.0}, "input_value_type": "raw_expression"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported_input"
    assert "matched reference" in body["reason"]
    assert body["increased_pathways"] == [] and body["decreased_pathways"] == []
