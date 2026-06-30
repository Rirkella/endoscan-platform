"""Regenerate the job-runner fixtures in the CONFIRMED real CoMPARA format.

Mirrors the real archive (server-inspected): ``compara_data.zip`` is a NESTED zip
``Data.zip -> Data/AR_data.zip -> {ToxCast_AR_Binding.sdf, ToxCast_AR_Agonist.sdf,
ToxCast_AR_Antagonist.sdf}`` plus a ``Predset_AR_QSAR-ready.sdf`` prediction file the
parser must EXCLUDE. Each SDF molecule carries the exact SD fields (incl. the literal
space in ``InChI Key_QSARr``) and the measured ``<Mode>Class`` label (1.0/0.0). The
LINCS metadata uses the SAME InChIKeys (derived via RDKit) so overlap is deterministic.

Compounds are arranged to exercise the mode logic:
- phenol: Binding=1 but Agonist=0 & Antagonist=0  -> functional_modulation NEGATIVE
  (binding EXCLUDED), broad_any_activity POSITIVE.  ← the critical contrast.
- toluene: Agonist=1 only -> functional POSITIVE (cross-mode union).
- nonylphenol: Antagonist=1 only -> functional POSITIVE.
- pyridine: Agonist {1.0, 0.0} -> intra-mode CONFLICT, excluded from agonist.

Run:  uv run python tests/fixtures/jobs/make_fixtures.py
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem
from rdkit.Chem import inchi as rd_inchi

RDLogger.DisableLog("rdApp.*")
HERE = Path(__file__).parent

SMILES = {
    "bisphenol_A": "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
    "nonylphenol": "CCCCCCCCCc1ccc(O)cc1",
    "phenol": "Oc1ccccc1",
    "toluene": "Cc1ccccc1",
    "aniline": "Nc1ccccc1",
    "benzene": "c1ccccc1",
    "pyridine": "c1ccncc1",
    "ethanol": "CCO",
    "predicted_only": "CCN",  # appears ONLY in the Predset file (must be excluded)
}

# Per-SDF measured records: (compound, <Mode>Class value). Duplicates allowed (conflict).
BINDING = [
    ("bisphenol_A", 1.0),
    ("nonylphenol", 1.0),
    ("phenol", 1.0),
    ("benzene", 0.0),
    ("ethanol", 0.0),
]
AGONIST = [
    ("bisphenol_A", 1.0),
    ("nonylphenol", 0.0),
    ("phenol", 0.0),
    ("toluene", 1.0),
    ("aniline", 0.0),
    ("benzene", 0.0),
    ("pyridine", 1.0),
    ("pyridine", 0.0),  # conflict
]
ANTAGONIST = [
    ("bisphenol_A", 0.0),
    ("nonylphenol", 1.0),
    ("phenol", 0.0),
    ("aniline", 1.0),
    ("benzene", 0.0),
    ("pyridine", 0.0),
]
PREDSET = [("predicted_only", 1.0)]

# LINCS profiling for the overlap step (UPPERCASE cell_id).
CELLS = {
    "bisphenol_A": ["VCAP", "MCF7", "A549"],
    "nonylphenol": ["VCAP", "LNCAP"],
    "phenol": ["VCAP", "MCF7"],
    "toluene": ["MCF7"],
    "aniline": ["A549"],
    "benzene": ["VCAP"],
    "pyridine": ["PC3"],
    "ethanol": ["PC3"],
}


def _mol(smiles: str):
    mol = Chem.MolFromSmiles(smiles)
    AllChem.Compute2DCoords(mol)
    return mol


def _ikey(smiles: str) -> str:
    return rd_inchi.MolToInchiKey(Chem.MolFromSmiles(smiles))


def _build_sdf(records: list[tuple[str, float]], mode_name: str) -> bytes:
    """Write a single-mode SDF (real field names) to bytes."""
    buf = io.StringIO()
    writer = Chem.SDWriter(buf)
    for i, (name, class_value) in enumerate(records):
        mol = _mol(SMILES[name])
        std_inchi = rd_inchi.MolToInchi(mol)
        ikey = rd_inchi.MolToInchiKey(mol)
        mol.SetProp("_Name", name)
        mol.SetProp("casrn", f"100-00-{i:02d}")
        mol.SetProp("cid", str(1000 + i))
        mol.SetProp("gsid", "0")
        mol.SetProp("dsstox_substance_id", f"DTXSID{i:05d}")
        mol.SetProp("preferred_name", name)
        mol.SetProp("Canonical_QSARr", SMILES[name])
        mol.SetProp("Salt_Solvent", "")
        mol.SetProp("InChI_Code_QSARr", std_inchi)
        mol.SetProp("InChI Key_QSARr", ikey)  # NOTE: literal space in the field name
        mol.SetProp(f"AUC.{mode_name}", "0.50")
        mol.SetProp(f"{mode_name}Class", f"{class_value:.1f}")  # the MEASURED label
        mol.SetProp("AC50_Calculated", "10.0")
        writer.write(mol)
    writer.close()
    return buf.getvalue().encode("utf-8")


def main() -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ToxCast_AR_Binding.sdf", _build_sdf(BINDING, "Binding"))
        zf.writestr("ToxCast_AR_Agonist.sdf", _build_sdf(AGONIST, "Agonist"))
        zf.writestr("ToxCast_AR_Antagonist.sdf", _build_sdf(ANTAGONIST, "Antagonist"))
        # A PREDICTION file inside the archive — must be excluded by name.
        zf.writestr("Predset_AR_QSAR-ready.sdf", _build_sdf(PREDSET, "Binding"))
    ar_data_zip = inner.getvalue()

    zip_path = HERE / "compara_data.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Data/AR_data.zip", ar_data_zip)  # the real nesting
        zf.writestr("Data/README.txt", "CoMPARA experimental AR data (fixture, real format).")
    print("wrote", zip_path)

    # LINCS metadata keyed by the SAME InChIKeys. ``sig_info`` mirrors the REAL GSE92742
    # 12-column schema, which carries BOTH a NUMERIC dose/time column (``pert_dose`` 10.0,
    # ``pert_time`` 24.0) AND a STRING quantity column (``pert_idose`` "10 µM",
    # ``pert_itime`` "24 h"). That coexistence is exactly what collided in the first real
    # extraction (rename pert_idose->pert_dose duplicated the numeric pert_dose), so the
    # fixture must reproduce it for CI to catch the regression.
    pert_rows, sig_rows, sig_n = [], [], 0
    for i, (name, cells) in enumerate(CELLS.items()):
        pert_id = f"BRD-K{i:05d}"
        pert_rows.append(
            {
                "pert_id": pert_id,
                "pert_iname": name,
                "pert_type": "trt_cp",
                "inchi_key": _ikey(SMILES[name]),
            }
        )
        for cell in cells:
            sig_n += 1
            sig_rows.append(
                {
                    "sig_id": f"SIG_{sig_n:04d}",
                    "pert_id": pert_id,
                    "pert_iname": name,
                    "pert_type": "trt_cp",
                    "cell_id": cell,
                    "pert_dose": "10.0",  # NUMERIC (the real file's preferred column)
                    "pert_dose_unit": "µM",
                    "pert_idose": "10 µM",  # STRING quantity (collides if renamed onto pert_dose)
                    "pert_time": "24.0",  # NUMERIC
                    "pert_time_unit": "h",
                    "pert_itime": "24 h",  # STRING quantity
                    "distil_id": f"{cell}_{pert_id}:{sig_n}",
                }
            )
    sig_rows.append(
        {
            "sig_id": "SIG_CTRL",
            "pert_id": "DMSO",
            "pert_iname": "DMSO",
            "pert_type": "ctl_vehicle",
            "cell_id": "VCAP",
            "pert_dose": "-666",
            "pert_dose_unit": "-666",
            "pert_idose": "-666",
            "pert_time": "24.0",
            "pert_time_unit": "h",
            "pert_itime": "24 h",
            "distil_id": "VCAP_DMSO:ctrl",
        }
    )
    lincs = HERE / "lincs"
    lincs.mkdir(exist_ok=True)
    _write_tsv(lincs / "pert_info.txt", pert_rows)
    _write_tsv(lincs / "sig_info.txt", sig_rows)
    print("wrote", lincs / "pert_info.txt", "and", lincs / "sig_info.txt")
    print(
        f"SDF compounds: binding={len(BINDING)} agonist={len(AGONIST)} "
        f"antagonist={len(ANTAGONIST)} | trt_cp sigs={sig_n}"
    )


def _write_tsv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
