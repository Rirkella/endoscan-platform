#!/usr/bin/env python3
"""Extract SOURCED gene descriptions into the frontend annotation artifact.

Reads gene SYMBOLS (default: the committed landmark schema) and fetches a short description
per gene from a REAL, redistributable source, writing
``apps/web/src/gene-annotations/descriptions.json`` as ``{SYMBOL: {description, source,
source_version, license}}``. Descriptions are NEVER hand-written, LLM-generated, or paraphrased —
each entry is the source text verbatim, attributed. A gene with no real source summary gets NO
entry (the UI shows links-only + "annotation unavailable"). Nothing is gap-filled.

License basis (recorded so the redistribution right is verifiable as of the retrieval date):

- **NCBI Gene** summary text is a work of the U.S. National Library of Medicine and is in the
  **US public domain** (see the NLM web policies, https://www.nlm.nih.gov/web_policies.html, and
  the NCBI policies, https://www.ncbi.nlm.nih.gov/home/about/policies/). Attribution is still
  recorded per entry (``source`` = "NCBI Gene", ``license`` = "public domain (NLM)").
- **HGNC** approved symbol/name data is released under **CC0 1.0** (public domain dedication; see
  https://www.genenames.org/about/). Used here only for the approved gene name, not claims.

Network note: this script requires outbound access to ``eutils.ncbi.nlm.nih.gov`` (E-utilities).
In an offline/blocked environment it CANNOT run — in that case the committed artifact stays empty
(links-only) and this script + its provenance note document how descriptions are sourced. It must
NOT be replaced by generated text.

Usage:
    python scripts/extract_gene_annotations.py [--symbols-file PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import datetime
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPO_ROOT / "models" / "ER" / "feature_schema.json"
OUT_PATH = REPO_ROOT / "apps" / "web" / "src" / "gene-annotations" / "descriptions.json"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_SOURCE = "NCBI Gene"
NCBI_LICENSE = "public domain (NLM)"
# NCBI etiquette: identify the tool; keep <= 3 requests/sec without an API key.
_PARAMS = {"tool": "endoscan", "email": "noreply@endoscan.example", "retmode": "json"}


def _get_json(path: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode({**_PARAMS, **params})
    url = f"{EUTILS}/{path}?{query}"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (trusted NCBI host)
        return json.loads(resp.read().decode("utf-8"))


def fetch_ncbi_summary(symbol: str) -> str | None:
    """Return the NCBI Gene summary for a human gene symbol, or None if there is no real summary."""
    term = f"{symbol}[sym] AND Homo sapiens[orgn]"
    found = _get_json("esearch.fcgi", {"db": "gene", "term": term})
    ids = found.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return None
    summary = _get_json("esummary.fcgi", {"db": "gene", "id": ids[0]})
    record = summary.get("result", {}).get(ids[0], {})
    text = (record.get("summary") or "").strip()
    return text or None  # omit genes with an empty summary — never fabricate


def load_symbols(symbols_file: Path) -> list[str]:
    data = json.loads(symbols_file.read_text(encoding="utf-8"))
    return list(data["features"]) if isinstance(data, dict) and "features" in data else list(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-file", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--limit", type=int, default=0, help="0 = all symbols")
    args = parser.parse_args()

    symbols = load_symbols(args.symbols_file)
    if args.limit:
        symbols = symbols[: args.limit]

    today = datetime.date.today().isoformat()
    genes: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        try:
            text = fetch_ncbi_summary(symbol)
        except Exception as exc:  # noqa: BLE001 — a fetch failure must not fabricate; just skip
            print(f"skip {symbol}: {exc}")
            continue
        if text:
            genes[symbol] = {
                "description": text,
                "source": NCBI_SOURCE,
                "source_version": today,
                "license": NCBI_LICENSE,
            }
        time.sleep(0.34)  # <= 3 req/sec

    artifact = {
        "_provenance": {
            "status": "populated" if genes else "empty",
            "generator": "scripts/extract_gene_annotations.py",
            "retrieved": today,
            "source_urls": [f"{EUTILS}/esearch.fcgi", f"{EUTILS}/esummary.fcgi"],
            "license_basis": {
                "ncbi_gene": {
                    "text_is": "US public domain (work of the U.S. National Library of Medicine)",
                    "policy_urls": [
                        "https://www.nlm.nih.gov/web_policies.html",
                        "https://www.ncbi.nlm.nih.gov/home/about/policies/",
                    ],
                },
                "hgnc": {
                    "text_is": "CC0 1.0 (public domain dedication)",
                    "policy_url": "https://www.genenames.org/about/",
                },
            },
        },
        "genes": genes,
    }
    OUT_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(genes)} sourced descriptions to {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
