"""Read the committed Explore data-space artifacts (read-only; NO science, NO fitting).

The heavy UMAP fit is a JOBS step (``build_explore_map``); it writes ``umap.json`` /
``support.npy`` / ``manifest.json`` under ``artifacts/<TARGET>/explore/`` in the jobs data
root. The validated real artifacts are transferred into ``models/<ENDPOINT>/explore/`` in
git (checksum-recorded, exactly like ``model.pkl``). This module only READS those committed
files, anchored at the app's ``repo_root``.

It never imports ``umap`` and never projects a new point: request-time placement (in
``routes/explore.py``) is exact k-NN over ``support.npy`` in the ORIGINAL gene space.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import numpy as np

#: Where committed explore artifacts live, per endpoint (beside ``feature_schema.json``).
EXPLORE_SUBDIR = "explore"
_CONTEXT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ExploreArtifactUnavailableError(FileNotFoundError):
    """No committed explore map for this context yet -> 404 ("not yet computed")."""


class ExploreArtifactCorruptError(RuntimeError):
    """A committed explore artifact failed its integrity gate (hash mismatch) -> 500."""


class ExploreContextError(ValueError):
    """A reference context identifier is invalid and cannot be resolved safely."""


class ExploreReferenceNotFoundError(LookupError):
    """A compound id is not represented by a row in the selected support matrix."""


def _explore_dir(repo_root: Path, context: str) -> Path:
    """Resolve a bounded context identifier beneath the repository's models directory."""
    if not _CONTEXT_ID.fullmatch(context):
        raise ExploreContextError("reference context must be a simple endpoint identifier")
    models_root = (repo_root / "models").resolve()
    base = (models_root / context / EXPLORE_SUBDIR).resolve()
    if not base.is_relative_to(models_root):
        raise ExploreArtifactCorruptError("explore artifact path escaped the models directory")
    return base


def load_map(repo_root: Path, context: str) -> tuple[dict, dict]:
    """Return ``(umap_doc, manifest)`` for ``context``; raise if no artifact is committed."""
    base = _explore_dir(repo_root, context)
    umap_path = base / "umap.json"
    manifest_path = base / "manifest.json"
    if not umap_path.is_file() or not manifest_path.is_file():
        raise ExploreArtifactUnavailableError(f"data-space map not yet computed for {context!r}")
    umap_doc = json.loads(umap_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return umap_doc, manifest


def load_support(repo_root: Path, context: str, manifest: dict) -> np.ndarray:
    """Load the original-space training matrix, verifying it matches the manifest hash.

    The source-hash gate ensures ``/locate`` never places a signature against a support
    matrix that has drifted from the map/manifest it is served with.
    """
    base = _explore_dir(repo_root, context)
    support_path = base / "support.npy"
    if not support_path.is_file():
        raise ExploreArtifactUnavailableError(f"data-space map not yet computed for {context!r}")
    raw = support_path.read_bytes()
    expected = manifest.get("support_sha256")
    if not expected or hashlib.sha256(raw).hexdigest() != expected:
        raise ExploreArtifactCorruptError(
            f"explore support artifact for {context!r} does not match its manifest hash"
        )
    return np.load(io.BytesIO(raw))


def load_reference_row(
    repo_root: Path, context: str, compound_id: str
) -> tuple[dict, dict, np.ndarray, int]:
    """Return the exact manifest-aligned support row for one reference compound.

    Row order is authoritative from ``manifest.compound_ids`` and feature order is
    authoritative from ``manifest.feature_names``. UMAP coordinates are never read here.
    """
    umap_doc, manifest = load_map(repo_root, context)
    support = load_support(repo_root, context, manifest)
    compound_ids = [str(value) for value in manifest.get("compound_ids", [])]
    feature_names = [str(value) for value in manifest.get("feature_names", [])]
    if support.shape != (len(compound_ids), len(feature_names)):
        raise ExploreArtifactCorruptError(
            f"explore support shape for {context!r} does not match manifest row/feature order"
        )
    try:
        row_index = compound_ids.index(compound_id)
    except ValueError as exc:
        raise ExploreReferenceNotFoundError(
            f"compound {compound_id!r} is not present in the {context!r} reference support matrix"
        ) from exc
    return umap_doc, manifest, support[row_index].copy(), row_index
