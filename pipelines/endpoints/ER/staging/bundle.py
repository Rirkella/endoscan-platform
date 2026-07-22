"""Assemble a review bundle (text artifacts + .dvc pointers) for handoff.

Copies a fixed list of repo-relative artifact paths into a bundle directory so a
maintainer can prepare a normal reviewed change. Pure file IO — no network, no git,
no science. Unit-tested on fixtures.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path


def assemble_review_bundle(
    src_root: Path, bundle_dir: Path, rel_paths: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Copy each existing ``src_root/rel`` into ``bundle_dir/rel`` (preserving layout).

    Returns ``(copied, missing)`` as lists of the relative paths. Missing paths are
    skipped rather than raising, so a not-yet-produced artifact never aborts the
    handoff — the caller surfaces ``missing`` to the operator.
    """
    src_root = Path(src_root)
    bundle_dir = Path(bundle_dir)
    copied: list[str] = []
    missing: list[str] = []
    for rel in rel_paths:
        src = src_root / rel
        if src.is_file():
            dst = bundle_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
        else:
            missing.append(rel)
    return copied, missing
