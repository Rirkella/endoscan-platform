"""Regenerate the DEMO_ER pickled model stub.

Run from the repo root:  uv run python tests/fixtures/registry/make_model_stub.py

The stub is a trusted, git-tracked plain dict — NOT a real estimator. M1's
`load_model` only deserializes it; it performs no inference.
"""

from __future__ import annotations

import pickle
from pathlib import Path

STUB = {
    "kind": "endoscan-demo-stub",
    "endpoint_id": "DEMO_ER",
    "n_features": 3,
    "features": ["GENE_A", "GENE_B", "GENE_C"],
}


def main() -> None:
    out = Path(__file__).parent / "models" / "DEMO_ER" / "model.pkl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        pickle.dump(STUB, handle, protocol=5)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
