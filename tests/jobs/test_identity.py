"""RDKit InChIKey derivation + per-structure label collapse."""

from __future__ import annotations

import pytest
from endoscan_jobs.identity import collapse_one_label_per_structure, inchikey_from


def test_inchikey_from_inchi_and_smiles_agree() -> None:
    pytest.importorskip("rdkit")
    inchi = "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"  # ethanol
    key_from_inchi = inchikey_from(inchi, None)
    key_from_smiles = inchikey_from(None, "CCO")
    assert key_from_inchi == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"  # standard ethanol InChIKey
    assert key_from_smiles == key_from_inchi  # both routes -> same standard key


def test_inchikey_from_garbage_is_none() -> None:
    pytest.importorskip("rdkit")
    assert inchikey_from("not-an-inchi", None) is None
    assert inchikey_from(None, "not a smiles !!!") is None
    assert inchikey_from(None, None) is None


def test_collapse_records_conflicts() -> None:
    clean, conflicts = collapse_one_label_per_structure(
        [("AAA", 1), ("AAA", 1), ("BBB", 0), ("CCC", 1), ("CCC", 0)]
    )
    assert clean == {"AAA": 1, "BBB": 0}  # CCC dropped (conflict)
    assert conflicts == 1
