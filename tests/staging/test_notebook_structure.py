"""Structural guards for the Phase-2b operator notebook (parsed via nbformat).

These catch the regressions that accumulated from repeated JSON patching:
- REPO/TOKEN must be defined in a preflight cell that PRECEDES any clone cell;
- no cell may contain an unauthenticated public-clone of the repo;
- exactly one Stop 4 section, and it is the bundle/no-push one;
- the four `## STOP n` section markers each appear exactly once, in order.
"""

from __future__ import annotations

import re
from pathlib import Path

import nbformat

NOTEBOOK = (
    Path(__file__).resolve().parents[2]
    / "pipelines"
    / "endpoints"
    / "ER"
    / "staging"
    / "colab_run_er_phase2.ipynb"
)


def _cells():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    return [(i, c.cell_type, c.source) for i, c in enumerate(nb.cells)]


def _code(cells):
    return [(i, src) for i, ctype, src in cells if ctype == "code"]


def test_preflight_defines_repo_and_token_before_any_clone() -> None:
    code = _code(_cells())
    clone_idx = [i for i, src in code if "git clone" in src or "git', 'clone'" in src]
    assert clone_idx, "expected a clone cell"
    preflight_idx = [
        i
        for i, src in code
        if re.search(r"^\s*REPO\s*=", src, re.M)
        and re.search(r"^\s*TOKEN\s*=", src, re.M)
        and "userdata" in src
    ]
    assert preflight_idx, "no preflight cell defines REPO + TOKEN via userdata"
    assert min(preflight_idx) < min(clone_idx), "preflight must precede the clone cell"


def test_no_unauthenticated_public_clone() -> None:
    # Any git clone of the repo must go through the GIT_ASKPASS/token mechanism.
    for i, src in _code(_cells()):
        if "git clone" in src or "git', 'clone'" in src:
            assert "GIT_ASKPASS" in src, f"cell {i} clones without the askpass/token mechanism"
    # The old literal unauthenticated form must be gone entirely.
    for _, _, src in _cells():
        assert "!git clone https://github.com/Rirkella/endoscan-platform.git" not in src


def test_exactly_one_stop4_bundle_section() -> None:
    cells = _cells()
    stop4 = [(i, src) for i, _, src in cells if re.search(r"^##\s*STOP 4\b", src, re.M)]
    assert len(stop4) == 1, f"expected exactly one '## STOP 4' section, found {len(stop4)}"
    # It must be the bundle/no-push variant, not an old push/commit one.
    nearby = "\n".join(src for _, _, src in cells if "STOP 4" in src or "review bundle" in src)
    assert "never pushes to GitHub" in nearby
    # The Stop-4 code cell assembles the bundle and does not push to GitHub.
    bundle_code = [src for _, src in _code(cells) if "assemble_review_bundle" in src]
    assert len(bundle_code) == 1, "expected exactly one bundle-assembly code cell"
    assert "git push" not in bundle_code[0]


def test_four_stop_markers_each_once_in_order() -> None:
    cells = _cells()
    positions: dict[int, int] = {}
    for i, _, src in cells:
        for n in (1, 2, 3, 4):
            if re.search(rf"^##\s*STOP {n}\b", src, re.M):
                assert n not in positions, f"'## STOP {n}' appears more than once"
                positions[n] = i
    assert sorted(positions) == [1, 2, 3, 4], f"missing/extra stop markers: {sorted(positions)}"
    order = [positions[n] for n in (1, 2, 3, 4)]
    assert order == sorted(order), f"stop markers out of order: {order}"


def test_inkernel_import_preflight_between_install_and_fetch() -> None:
    code = _code(_cells())
    install_idx = [
        i for i, src in code if re.search(r"pip install.*-e\s+packages/endoscan_core", src)
    ]
    fetch_idx = [i for i, src in code if re.search(r"#\s*1c\)", src)]
    preflight = [i for i, src in code if "endoscan_core import OK" in src]
    assert install_idx, "no editable-install cell for endoscan_core"
    assert fetch_idx, "no 1c fetch cell"
    assert len(preflight) == 1, "expected exactly one in-kernel import-preflight cell"
    install_i, fetch_i, pf_i = min(install_idx), min(fetch_idx), preflight[0]
    assert install_i < pf_i < fetch_i, "import-preflight must be AFTER install and BEFORE 1c fetch"

    pf_src = dict(code)[pf_i]
    # In-kernel import — NOT a `!python -c "import ..."` subprocess (which would falsely pass).
    assert re.search(r"^\s*import endoscan_core", pf_src, re.M), "preflight must import in-kernel"
    # No `!`-prefixed shell line that runs `python -c` (a comment mentioning it is fine).
    assert not re.search(
        r"^\s*!.*python.*-c\b", pf_src, re.M
    ), "preflight must not shell out to a `!python -c` subprocess import check"
