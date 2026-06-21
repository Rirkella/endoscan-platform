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
