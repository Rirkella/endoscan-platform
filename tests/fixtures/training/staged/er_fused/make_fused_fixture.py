"""Regenerate the FUSED (compound-level) staged ER fixture (offline).

Run from the repo root:
    uv run python tests/fixtures/training/staged/er_fused/make_fused_fixture.py

Unlike ``staged/er`` (which is SIGNATURE-level: sig_id/pert_id + genes), this fixture
mirrors the REAL post-approval staged shape: ``lincs.parquet`` is a COMPOUND-LEVEL fused
matrix — one row per compound, an ``compound_id`` column holding the full InChIKey plus
numeric landmark-gene columns (MCF7/A549 already mean-fused). The CERAPP-style labels
(``cerapp.csv``) are keyed by CASRN and resolved to the SAME InChIKey via the mapping
table (``pubchem.csv``: CASRN -> InChIKey), so labels and signatures join on InChIKey.

10 compounds: 6 active / 4 inactive. The gene values carry no class signal — this
fixture exists to exercise the dataset layer fused-matrix CONTRACT (overlap > 0, per-class counts,
ID excluded from features), not to train a meaningful model.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
GENES = ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]
N_COMPOUNDS = 10
N_ACTIVE = 6  # first 6 active, last 4 inactive
# Class-independent gene patterns (no signal), cycled across compounds.
PATTERNS = [
    [1.5, 1.4, 1.3, 1.2, 1.1],
    [-1.5, -1.4, -1.3, -1.2, -1.1],
    [1.0, -1.0, 1.0, -1.0, 1.0],
    [0.1, 0.0, -0.1, 0.05, -0.05],
]


def _inchikey(i: int) -> str:
    """A full 27-char-shaped InChIKey unique per compound (block1-block2-flag)."""
    block = chr(ord("A") + (i % 26))
    return f"{block * 14}-{block * 10}-N"


def main() -> None:
    sig_rows = []
    label_rows = []
    map_rows = []

    for idx in range(N_COMPOUNDS):
        active = idx < N_ACTIVE
        call = "active" if active else "inactive"
        casrn = f"300-00-{idx:02d}"
        inchikey = _inchikey(idx)
        pattern = PATTERNS[idx % len(PATTERNS)]

        # Fused: one row per compound, ID column = compound_id (full InChIKey).
        sig_rows.append({"compound_id": inchikey, **dict(zip(GENES, pattern, strict=True))})
        # CERAPP-style label row (assay collapsed upstream): keyed by CASRN.
        label_rows.append(
            {
                "casrn": casrn,
                "inchikey": inchikey,  # derived upstream (provenance); join is via mapping
                "inchi_code": f"InChI=1S/cpd{idx}",
                "target": "ER",
                "consensus_call": call,
                "consensus_score": 0.9,
                "label": 1 if active else 0,
                "label_provenance": "cerapp_experimental_binding",
                "inchikey_source": "inchi",
            }
        )
        # Mapping: CASRN -> the SAME InChIKey, so labels resolve to the fused identity.
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

    pd.DataFrame(sig_rows).to_parquet(HERE / "lincs.parquet", index=False)
    pd.DataFrame(label_rows).to_csv(HERE / "cerapp.csv", index=False)
    pd.DataFrame(map_rows).to_csv(HERE / "pubchem.csv", index=False)
    # Other ER label sources are approved but contribute no rows here (header only).
    pd.DataFrame(
        columns=["dsstox_sid", "casrn", "preferred_name", "assay_name", "target", "hitcall"]
    ).to_csv(HERE / "toxcast.csv", index=False)
    pd.DataFrame(
        columns=["sample_id", "casrn", "pubchem_cid", "assay_name", "target", "activity"]
    ).to_csv(HERE / "tox21.csv", index=False)
    print(f"wrote FUSED staged ER fixtures to {HERE}")


if __name__ == "__main__":
    main()
