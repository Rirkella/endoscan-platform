"""Regenerate the staged ER nested-CV test fixtures (offline, weak-signal).

Run from the repo root:
    uv run python tests/fixtures/training/staged/er/make_staged_er_fixture.py

Produces a small STAGED-FORMAT dataset (Parquet signatures + CSV label/mapping)
large enough for nested grouped CV (16 compounds: 8 active, 8 inactive), with
landmark-gene values that carry NO class signal — each gene pattern (high / low /
alt / mid) appears equally in both classes — so the cross-validated AUROC sits
near 0.5 and the end-to-end run deterministically registers ER as `experimental`.
This is a documented stand-in; the real ER (reviewed real-data workflow) uses real staged data.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
GENES = ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]
# Four class-independent gene patterns.
PATTERNS = {
    0: [1.5, 1.4, 1.3, 1.2, 1.1],
    1: [-1.5, -1.4, -1.3, -1.2, -1.1],
    2: [1.0, -1.0, 1.0, -1.0, 1.0],
    3: [0.1, 0.0, -0.1, 0.05, -0.05],
}


def _inchikey(i: int) -> str:
    block = chr(ord("A") + (i % 26))
    return f"{block * 14}-{block * 10}-N"


def main() -> None:
    sig_rows = []
    label_rows = []
    map_rows = []

    for idx in range(16):
        active = idx < 8  # first 8 active, last 8 inactive
        label = 1 if active else 0
        n = idx % 8  # 0..7 within each class
        casrn = f"300-00-{idx:02d}"
        pert = f"BRD-E{idx:02d}"
        inchikey = _inchikey(idx)
        pattern = PATTERNS[n % 4]

        sig_rows.append(
            {
                "sig_id": f"S{idx:02d}",
                "pert_id": pert,
                "pert_iname": f"cpd{idx:02d}",
                "cell_id": "MCF7",
                "pert_dose": 10.0,
                "pert_dose_unit": "uM",
                "pert_time": 24.0,
                "pert_time_unit": "h",
                **dict(zip(GENES, pattern, strict=True)),
            }
        )
        label_rows.append(
            {
                "dsstox_sid": f"DTXSID{idx:02d}",
                "casrn": casrn,
                "preferred_name": f"cpd{idx:02d}",
                "assay_name": "ATG_ERa_TRANS",
                "target": "ER",
                "hitcall": label,
                "ac50": None,
                "ac50_unit": None,
            }
        )
        map_rows.append(
            {
                "input_id": casrn,
                "input_id_type": "CASRN",
                "inchikey": inchikey,
                "cid": 9000 + idx,
                "smiles": f"c1ccccc1{idx}",
                "mapping_confidence": "exact",
            }
        )
        map_rows.append(
            {
                "input_id": pert,
                "input_id_type": "PERT_ID",
                "inchikey": inchikey,
                "cid": 9000 + idx,
                "smiles": f"c1ccccc1{idx}",
                "mapping_confidence": "exact",
            }
        )

    pd.DataFrame(sig_rows).to_parquet(HERE / "lincs.parquet", index=False)
    pd.DataFrame(label_rows).to_csv(HERE / "toxcast.csv", index=False)
    pd.DataFrame(map_rows).to_csv(HERE / "pubchem.csv", index=False)
    # Other ER label sources exist but contribute no rows here (header only).
    pd.DataFrame(
        columns=["sample_id", "casrn", "pubchem_cid", "assay_name", "target", "activity", "potency"]
    ).to_csv(HERE / "tox21.csv", index=False)
    pd.DataFrame(
        columns=[
            "casrn",
            "chemical_name",
            "inchikey",
            "target",
            "consensus_call",
            "consensus_score",
        ]
    ).to_csv(HERE / "cerapp.csv", index=False)
    print(f"wrote staged ER fixtures to {HERE}")


if __name__ == "__main__":
    main()
