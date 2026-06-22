"""PubChem/UniChem mapping normalization."""

from __future__ import annotations

import pubchem  # noqa: E402 — resolved via conftest sys.path


def test_guess_casrn_column() -> None:
    assert pubchem.guess_casrn_column(["DTXSID", "CASRN", "name"]) == "CASRN"
    assert pubchem.guess_casrn_column(["casn", "x"]) == "casn"
    assert pubchem.guess_casrn_column(["foo", "bar"]) is None


def test_normalize_mapping_defaults_and_drops() -> None:
    rows = [
        {"input_id": "1-1-1", "input_id_type": "CASRN", "inchikey": "AAA", "cid": 1, "smiles": "C"},
        {"input_id": "x", "InChIKey": "BBB"},  # type defaults to CASRN, smiles missing
        {"inchikey": "CCC"},  # no input_id -> dropped
    ]
    out = pubchem.normalize_mapping(rows)
    assert len(out) == 2
    assert out[0]["inchikey"] == "AAA"
    assert out[1]["input_id_type"] == "CASRN"  # default
    assert out[1]["inchikey"] == "BBB"
