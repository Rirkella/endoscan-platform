#!/usr/bin/env python3
"""Extract REAL Reactome pathway → gene-symbol sets into the committed enrichment artifact.

Reads Reactome's own download files and writes ``data/reactome/reactome_pathways.json`` as
``{reactome_version, source_url, retrieval_date, license, n_pathways, n_genes, pathways:[
{id, name, genes:[SYMBOL...]}]}``. Gene sets are taken VERBATIM from Reactome — never
hand-authored, generated, or gap-filled. A pathway with no human gene members gets no entry.

Inputs (Reactome download-data, Homo sapiens):
  --pathways   ReactomePathways.txt            [stable_id, name, species]
  --mapping    NCBI2Reactome_All_Levels.txt    [entrez_id, reactome_id, url, name, evidence, sp]
  --gene-info  a symbol map with pr_gene_id/pr_gene_symbol (e.g. the staged LINCS gene_info),
               used ONLY to translate Reactome's Entrez ids to the symbol space the models use.

License basis (recorded so redistribution is verifiable as of the retrieval date):
  Reactome content is released under **CC0 1.0** (public-domain dedication) — see
  https://reactome.org/license. Recorded per artifact, not assumed; re-verify at retrieval time.

Network note: this requires outbound access to reactome.org (or locally-staged copies of the
files above). In the blocked agent environment it CANNOT run — the committed artifact then stays
ABSENT and the API returns an honest "pathway data not available"; this script + its provenance
document how the real artifact is produced. It must NEVER be replaced by fabricated mappings.

Usage:
    python scripts/extract_reactome_pathways.py \
        --pathways ReactomePathways.txt \
        --mapping  NCBI2Reactome_All_Levels.txt \
        --gene-info gene_info.txt \
        [--species "Homo sapiens"] [--reactome-version "<release>"]
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "reactome" / "reactome_pathways.json"

SOURCE_URL = "https://reactome.org/download-data"
LICENSE = "CC0 1.0 (public domain dedication) — per https://reactome.org/license"


def _entrez_to_symbol(gene_info: Path) -> dict[str, str]:
    """Map Entrez gene id -> HGNC symbol from a gene_info TSV (pr_gene_id / pr_gene_symbol)."""
    mapping: dict[str, str] = {}
    with gene_info.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            gid = str(row.get("pr_gene_id", "")).strip()
            sym = str(row.get("pr_gene_symbol", "")).strip()
            if gid and sym:
                mapping[gid] = sym
    return mapping


def _human_pathway_names(pathways_file: Path, species: str) -> dict[str, str]:
    names: dict[str, str] = {}
    with pathways_file.open(encoding="utf-8") as fh:
        for stable_id, name, sp in csv.reader(fh, delimiter="\t"):
            if sp.strip() == species:
                names[stable_id.strip()] = name.strip()
    return names


def build(pathways_file: Path, mapping_file: Path, gene_info: Path, species: str) -> dict:
    names = _human_pathway_names(pathways_file, species)
    e2s = _entrez_to_symbol(gene_info)
    genes_by_pathway: dict[str, set[str]] = {}
    with mapping_file.open(encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            # NCBI2Reactome_All_Levels: entrez, reactome_id, url, event_name, evidence, species
            if len(row) < 6 or row[5].strip() != species:
                continue
            entrez, reactome_id = row[0].strip(), row[1].strip()
            if reactome_id not in names:
                continue
            symbol = e2s.get(entrez)
            if symbol:  # only real, mappable symbols — never a placeholder
                genes_by_pathway.setdefault(reactome_id, set()).add(symbol)

    pathways = [
        {"id": pid, "name": names[pid], "genes": sorted(genes)}
        for pid, genes in sorted(genes_by_pathway.items())
        if genes
    ]
    all_genes = sorted({g for p in pathways for g in p["genes"]})
    return {
        "reactome_version": None,  # set via --reactome-version at retrieval
        "source_url": SOURCE_URL,
        "retrieval_date": datetime.date.today().isoformat(),
        "license": LICENSE,
        "n_pathways": len(pathways),
        "n_genes": len(all_genes),
        "pathways": pathways,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pathways", required=True, type=Path)
    ap.add_argument("--mapping", required=True, type=Path)
    ap.add_argument("--gene-info", required=True, type=Path)
    ap.add_argument("--species", default="Homo sapiens")
    ap.add_argument("--reactome-version", default=None)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    artifact = build(args.pathways, args.mapping, args.gene_info, args.species)
    if args.reactome_version:
        artifact["reactome_version"] = args.reactome_version
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"wrote {args.out}: {artifact['n_pathways']} pathways, {artifact['n_genes']} genes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
