"""CoMPARA parser: SDF real-format path (nested zip, modes, union) + table fallback."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from endoscan_jobs.compara import (
    DIAGNOSTIC_ONLY_MODES,
    MODES,
    NoMeasuredCallError,
    extract_sdf_records,
    extract_tables,
    is_sdf_archive,
    labels_for_mode,
    select_measured_ar_call,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ZIP = REPO_ROOT / "tests" / "fixtures" / "jobs" / "compara_data.zip"

pytest.importorskip("rdkit")  # the SDF path derives InChIKeys


def _ikey(smiles: str) -> str:
    from rdkit import Chem
    from rdkit.Chem import inchi as rd_inchi

    return rd_inchi.MolToInchiKey(Chem.MolFromSmiles(smiles))


# --- SDF real format ------------------------------------------------------------------


def test_is_sdf_archive_detects_nested_sdf() -> None:
    assert is_sdf_archive(FIXTURE_ZIP) is True
    # A plain CSV zip is NOT an SDF archive.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("t.csv", "InChI,AR_Binding\nInChI=1S/CH4/h1H4,Active\n")
    assert is_sdf_archive(buf.getvalue()) is False


def test_nested_zip_sdf_extraction_excludes_predset() -> None:
    recs = extract_sdf_records(FIXTURE_ZIP)  # Data.zip -> Data/AR_data.zip -> 3 SDFs
    assert {m: len(v) for m, v in recs.items()} == {"binding": 5, "agonist": 8, "antagonist": 6}
    # The Predset_*.sdf prediction file is NEVER read as a label.
    all_iks = {r.inchikey for v in recs.values() for r in v}
    assert _ikey("CCN") not in all_iks  # predicted_only


def test_single_mode_labels() -> None:
    recs = extract_sdf_records(FIXTURE_ZIP)
    binding = labels_for_mode(recs, "binding")
    assert binding.labels[_ikey("Oc1ccccc1")] == 1  # phenol binding-active
    assert binding.n_positive == 3 and binding.n_negative == 2 and not binding.diagnostic_only


def test_functional_modulation_excludes_binding() -> None:
    # ★ A binding-ONLY positive (phenol) must NOT be positive under functional_modulation —
    # binding is receptor attachment, functional = agonist∪antagonist (different claim).
    recs = extract_sdf_records(FIXTURE_ZIP)
    fm = labels_for_mode(recs, "functional_modulation")
    phenol = _ikey("Oc1ccccc1")
    assert fm.labels[phenol] == 0  # present, but NEGATIVE (binding=1 is NOT consulted)
    assert labels_for_mode(recs, "binding").labels[phenol] == 1  # contrast: binding-positive
    assert labels_for_mode(recs, "broad_any_activity").labels[phenol] == 1  # broad includes binding


def test_functional_modulation_cross_mode_union_positive() -> None:
    recs = extract_sdf_records(FIXTURE_ZIP)
    fm = labels_for_mode(recs, "functional_modulation")
    assert fm.labels[_ikey("Cc1ccccc1")] == 1  # toluene: agonist=1 only -> union positive
    assert fm.labels[_ikey("CCCCCCCCCc1ccc(O)cc1")] == 1  # nonylphenol: antagonist=1 only
    assert fm.n_positive == 4 and fm.n_negative == 3


def test_intra_mode_conflict_excluded() -> None:
    # pyridine appears in the agonist SDF as both 1.0 and 0.0 -> conflict, excluded.
    recs = extract_sdf_records(FIXTURE_ZIP)
    agonist = labels_for_mode(recs, "agonist")
    assert _ikey("c1ccncc1") not in agonist.labels
    assert agonist.n_conflicts == 1
    # The conflict propagates to functional's conflict count (cross-mode is NOT a conflict).
    assert labels_for_mode(recs, "functional_modulation").n_conflicts == 1


def test_inchikey_derived_from_mol_block() -> None:
    recs = extract_sdf_records(FIXTURE_ZIP)
    binding_keys = {r.inchikey for r in recs["binding"]}
    assert _ikey("Oc1ccccc1") in binding_keys  # InChIKey from the embedded mol block


def test_broad_any_activity_is_diagnostic_only() -> None:
    recs = extract_sdf_records(FIXTURE_ZIP)
    broad = labels_for_mode(recs, "broad_any_activity")
    assert broad.diagnostic_only is True and "broad_any_activity" in DIAGNOSTIC_ONLY_MODES
    assert broad.n_positive == 5  # adds phenol (binding) on top of the 4 functional positives


def test_spaced_inchikey_field_present_in_sdf() -> None:
    # The real archive uses a field name with a literal space: 'InChI Key_QSARr'.
    from rdkit import Chem

    raw = FIXTURE_ZIP.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as outer:
        inner = outer.read("Data/AR_data.zip")
    with zipfile.ZipFile(io.BytesIO(inner)) as z:
        sdf = z.read("ToxCast_AR_Binding.sdf")
    mol = next(m for m in Chem.ForwardSDMolSupplier(io.BytesIO(sdf)) if m is not None)
    assert "InChI Key_QSARr" in mol.GetPropsAsDict()  # spaced field round-trips


def test_modes_constant() -> None:
    assert MODES == (
        "binding",
        "agonist",
        "antagonist",
        "functional_modulation",
        "broad_any_activity",
    )


# --- table fallback (inline CSV zip; not the fixture, which is now SDF) -----------------


def _csv_zip(csv_text: str, name: str = "t.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, csv_text)
    return buf.getvalue()


def test_table_path_selects_measured_not_predicted() -> None:
    raw = _csv_zip(
        "SMILES,AR_Binding,AR_Binding_consensus_pred\n"
        "CCO,Active,Inactive\n"
        "c1ccccc1,Inactive,Active\n"
    )
    tables = extract_tables(raw)
    selection, _df = select_measured_ar_call(tables)
    assert selection.call_col == "AR_Binding"  # measured, not the *_consensus_pred decoy


def test_table_path_predicted_only_rejected() -> None:
    df = pd.DataFrame({"InChI": ["InChI=1S/CH4/h1H4"], "AR_Binding_consensus_pred": ["Active"]})
    with pytest.raises(NoMeasuredCallError):
        select_measured_ar_call([("preds.csv", df)])
