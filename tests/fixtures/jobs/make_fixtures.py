"""Regenerate the job-runner fixtures (CoMPARA Data.zip + LINCS metadata).

PLAUSIBLE CoMPARA structure (the real 68 KB Data.zip could not be inspected from the
build sandbox — figshare/clowder are egress-blocked — so this mirrors the expected
training-set shape and is to be confirmed against the real archive at server-wiring).

The CoMPARA csv carries real InChI strings, a measured ``AR_Binding`` call, and a DECOY
predicted column (``AR_Binding_consensus_pred``) the parser must EXCLUDE. The LINCS
pert_info uses the SAME InChIKeys (derived here via RDKit) so overlap is deterministic.

Run:  uv run python tests/fixtures/jobs/make_fixtures.py
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import inchi as rd_inchi

RDLogger.DisableLog("rdApp.*")
HERE = Path(__file__).parent

# (name, SMILES, AR_Binding measured call, LINCS cell lines it is profiled in)
COMPOUNDS = [
    ("benzene", "c1ccccc1", "Inactive", ["VCAP"]),
    ("toluene", "Cc1ccccc1", "Inactive", ["MCF7"]),
    ("phenol", "Oc1ccccc1", "Active", ["VCAP", "MCF7"]),
    ("aniline", "Nc1ccccc1", "Inactive", ["A549"]),
    ("ethanol", "CCO", "Inactive", ["PC3"]),
    ("acetone", "CC(=O)C", "Inactive", ["MCF7"]),
    ("bisphenol_A", "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1", "Active", ["VCAP", "MCF7", "A549"]),
    ("naphthalene", "c1ccc2ccccc2c1", "Active", ["VCAP"]),
    ("pyridine", "c1ccncc1", "Inactive", ["PC3"]),
    ("benzoic_acid", "OC(=O)c1ccccc1", "Active", ["A549"]),
    ("acetic_acid", "CC(=O)O", "Inactive", ["MCF7"]),
    ("nonylphenol", "CCCCCCCCCc1ccc(O)cc1", "Active", ["VCAP", "LNCAP"]),
]


def _inchi_key(smiles: str) -> tuple[str, str]:
    mol = Chem.MolFromSmiles(smiles)
    return rd_inchi.MolToInchi(mol), rd_inchi.MolToInchiKey(mol)


def main() -> None:
    rows = []
    pert_rows = []
    sig_rows = []
    sig_n = 0
    for i, (name, smiles, call, cells) in enumerate(COMPOUNDS):
        std_inchi, ikey = _inchi_key(smiles)
        casrn = f"100-00-{i:02d}"
        # CoMPARA training-set row: measured AR_Binding + a DECOY predicted column.
        rows.append(
            {
                "DTXSID": f"DTXSID{i:05d}",
                "CASRN": casrn,
                "PREFERRED_NAME": name,
                "SMILES": smiles,
                "InChI": std_inchi,
                "AR_Binding": call,  # MEASURED — the parser must pick this
                "AR_Agonist": "Inactive",
                "AR_Binding_consensus_pred": "Active",  # PREDICTED decoy — must be EXCLUDED
            }
        )
        pert_id = f"BRD-K{i:05d}"
        pert_rows.append(
            {"pert_id": pert_id, "pert_iname": name, "pert_type": "trt_cp", "inchi_key": ikey}
        )
        for cell in cells:
            sig_n += 1
            sig_rows.append(
                {
                    "sig_id": f"SIG_{sig_n:04d}",
                    "pert_id": pert_id,
                    "pert_type": "trt_cp",
                    "cell_id": cell,
                    "pert_idose": "10 uM",
                    "pert_itime": "24 h",
                }
            )
    # A non-trt_cp control row that must be ignored by the trt_cp filter.
    sig_rows.append(
        {
            "sig_id": "SIG_CTRL",
            "pert_id": "DMSO",
            "pert_type": "ctl_vehicle",
            "cell_id": "VCAP",
            "pert_idose": "-666",
            "pert_itime": "24 h",
        }
    )

    # CoMPARA Data.zip: a single inner csv (mirrors a small ~training-set archive).
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    zip_path = HERE / "compara_data.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("CoMPARA_Experimental_TrainingSet.csv", csv_buf.getvalue())
        zf.writestr("README.txt", "CoMPARA experimental AR training set (fixture).")
    print("wrote", zip_path)

    lincs = HERE / "lincs"
    lincs.mkdir(exist_ok=True)
    _write_tsv(lincs / "pert_info.txt", pert_rows)
    _write_tsv(lincs / "sig_info.txt", sig_rows)
    print("wrote", lincs / "pert_info.txt", "and", lincs / "sig_info.txt")
    print("compounds:", len(rows), "| trt_cp sigs:", sig_n)


def _write_tsv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
