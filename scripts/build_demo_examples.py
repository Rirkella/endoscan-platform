"""Build downloadable CSV/TSV examples from committed real LINCS demo signatures."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMOS = ROOT / "apps" / "web" / "src" / "demo-signatures"
OUTPUT = ROOT / "apps" / "web" / "public" / "examples"

EXAMPLES = (
    ("demo_low_low.json", "lincs-caffeic-acid-mcf7-a549.csv", ","),
    ("demo_er_high.json", "lincs-cid-450-mcf7-a549.tsv", "\t"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "schema_version": "1.0.0",
        "description": (
            "Downloadable real measured LINCS Level 5 examples. Values are copied unchanged "
            "from the committed curated demo signatures; only the tabular serialization differs."
        ),
        "examples": [],
    }
    records: list[dict[str, object]] = []
    for source_name, output_name, delimiter in EXAMPLES:
        source = DEMOS / source_name
        doc = json.loads(source.read_text(encoding="utf-8"))
        output = OUTPUT / output_name
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
            writer.writerow(("gene", "value"))
            writer.writerows(sorted(doc["signature"].items()))
        records.append(
            {
                "file": output_name,
                "format": "tsv" if delimiter == "\t" else "csv",
                "demo_id": doc["id"],
                "label": doc["label"],
                "n_genes": len(doc["signature"]),
                "provenance": doc["provenance"],
                "source_file": f"apps/web/src/demo-signatures/{source_name}",
                "source_sha256": _sha256(source),
                "file_sha256": _sha256(output),
                "transform": "gene/value rows sorted by gene symbol; numeric values unchanged",
            }
        )
    manifest["examples"] = records
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
