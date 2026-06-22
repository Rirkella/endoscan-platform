"""Network fetchers for the ER staging layer (run in the CLOUD, never in CI).

Downloads ONLY from the approved locators in ``registry/data/sources.yaml``. This
is the explicit offline prep layer: the toolchain never imports it, and
``RealDownloadAdapter`` stays a stub. Requires ``requests`` (and, for the clue.io
try-first path, a free API key). Not imported by the test suite.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def fetch_url(url: str, dest: Path, *, chunk: int = 1 << 20) -> Path:
    """Stream-download an approved URL to ``dest`` (cloud only)."""
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with dest.open("wb") as handle:
            for part in resp.iter_content(chunk_size=chunk):
                handle.write(part)
    return dest


def pubchem_mapping_for_casrns(casrns: Sequence[str]) -> list[dict]:
    """Build CASRN -> InChIKey/CID/SMILES rows via PubChem PUG REST (cloud only).

    Returns rows shaped for ``pubchem.normalize_mapping``. Runs only in the Colab
    run (network). Unmapped CASRNs are skipped. Not imported by the test suite.
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"
    props = "InChIKey,CanonicalSMILES"
    rows: list[dict] = []
    for casrn in casrns:
        url = f"{base}/{casrn}/property/{props}/JSON"
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                continue
            prop = resp.json()["PropertyTable"]["Properties"][0]
        except Exception:
            continue
        rows.append(
            {
                "input_id": str(casrn),
                "input_id_type": "CASRN",
                "inchikey": prop.get("InChIKey"),
                "cid": prop.get("CID"),
                "smiles": prop.get("CanonicalSMILES"),
                "mapping_confidence": "exact",
            }
        )
    return rows


def figshare_file_urls(article_id: str) -> list[tuple[str, str]]:
    """Return ``[(filename, download_url)]`` for a figshare article (cloud only).

    Hits the public figshare API ``GET /v2/articles/{article_id}`` and reads
    ``files[].download_url``. Not imported by the test suite.
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    resp = requests.get(f"https://api.figshare.com/v2/articles/{article_id}", timeout=60)
    resp.raise_for_status()
    return [(f["name"], f["download_url"]) for f in resp.json().get("files", [])]


def fetch_cerapp_experimental(
    dest_dir: Path, article_ids: Sequence[str], *, gaftp_url: str | None = None
) -> list[Path]:
    """Resolve + download the CERAPP EXPERIMENTAL files programmatically (cloud only).

    Tries the figshare API first (the ``article_ids`` parsed from the approved
    figshare locators), downloading every file each article exposes. If that yields
    nothing and ``gaftp_url`` is given, streams the gaftp mirror instead. Raises if
    neither resolves (the notebook then offers a single manual-upload fallback).
    Returns the local file paths. Not imported by the test suite.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    try:
        for article_id in article_ids:
            for name, download_url in figshare_file_urls(article_id):
                paths.append(fetch_url(download_url, dest_dir / name))
    except Exception:
        paths = []
    if not paths and gaftp_url:
        name = Path(gaftp_url.rstrip("/")).name or "cerapp_gaftp"
        paths = [fetch_url(gaftp_url, dest_dir / name)]
    if not paths:
        raise RuntimeError(
            "Could not resolve CERAPP experimental files from figshare or the gaftp "
            "mirror; use the clearly-marked manual-upload fallback cell."
        )
    return paths


def read_cerapp_table(paths: Sequence[Path]):
    """Read the first tabular CERAPP file (csv/tsv/txt/xlsx) into a DataFrame (cloud only)."""
    import pandas as pd  # noqa: PLC0415 — staging-only

    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix in {".tsv", ".txt"}:
            return pd.read_csv(path, sep="\t")
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
    raise RuntimeError(f"No tabular CERAPP file among: {[str(p) for p in paths]}")


def clue_io_landmark_signatures(sig_ids: Sequence[str], api_key: str):
    """TRY-FIRST optimization: fetch per-``sig_id`` Level-5 landmark vectors.

    The clue.io Data API surface for per-signature MODZ landmark values is
    historically gated and UNVERIFIED from the build environment. The Colab run
    may attempt it; on any failure the caller falls back to the GEO gctx slice
    (the supported backbone). Kept as a stub so 2a ships without depending on it.
    """
    raise NotImplementedError(
        "clue.io per-sig_id landmark fetch is the try-first optimization; implement/attempt "
        "it in the Colab run with a free key, and fall back to the gctx slice on failure."
    )
