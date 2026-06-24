"""Structural guards for the CLEAN ER Phase-2 operator notebook (parsed via nbformat).

The old hand-patched notebook (pipelines/endpoints/ER/staging/colab_run_er_phase2.ipynb)
was deleted; this is the single source of truth. These guards enforce the clean
contract and lock OUT the debugging-hotfix patterns that accumulated before:
- section/marker order Preflight -> Setup -> STOP 1 -> 2a -> 2b -> 2c -> 2d -> STOP 3/4;
- approval hard-coded False in 2d (training impossible in Stop 2);
- the heavy gctx download lives ONLY in 2c, after the 2b guard, which reads min_overlap;
- token-safe clone + in-kernel import preflight;
- no banned hotfix patterns in any CODE cell (no _parse_lincs / monkeypatch / setattr /
  compound_key rename / toxcast|tox21 staged identifiers / any compound_key token).
"""

from __future__ import annotations

import re
from pathlib import Path

import nbformat

NOTEBOOK = Path(__file__).resolve().parents[2] / "notebooks" / "colab_run_er_phase2_clean.ipynb"


def _cells():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    return [(i, c.cell_type, c.source) for i, c in enumerate(nb.cells)]


def _code(cells):
    return [(i, src) for i, ctype, src in cells if ctype == "code"]


def _code_sources(cells):
    return [src for _, src in _code(cells)]


def _one(pairs, pattern, label):
    """Index of the single cell whose source matches ``pattern`` (asserts exactly one)."""
    hits = [i for i, src in pairs if re.search(pattern, src, re.M)]
    assert len(hits) == 1, f"expected exactly one {label} cell, found {len(hits)}"
    return hits[0]


def test_section_and_stop_order() -> None:
    cells = _cells()
    code = _code(cells)
    md = [(i, src) for i, ctype, src in cells if ctype == "markdown"]

    order = [
        _one(code, r"#\s*Preflight\)", "Preflight"),
        _one(code, r"#\s*Setup-clone\)", "Setup-clone"),
        _one(md, r"^##\s*STOP 1\b", "STOP 1 header"),
        _one(code, r"#\s*2a\)", "2a"),
        _one(code, r"#\s*2b\)", "2b"),
        _one(code, r"#\s*2c\)", "2c"),
        _one(code, r"#\s*2d\)", "2d"),
        _one(md, r"^##\s*STOP 3\b", "STOP 3 header"),
        _one(md, r"^##\s*STOP 4\b", "STOP 4 header"),
    ]
    assert order == sorted(order), f"sections out of order: {order}"


def test_four_stop_markers_each_once_in_order() -> None:
    cells = _cells()
    positions: dict[int, int] = {}
    for i, _, src in cells:
        for n in (1, 2, 3, 4):
            if re.search(rf"^##\s*STOP {n}\b", src, re.M):
                assert n not in positions, f"'## STOP {n}' appears more than once"
                positions[n] = i
    assert sorted(positions) == [1, 2, 3, 4], f"missing/extra stop markers: {sorted(positions)}"
    assert [positions[n] for n in (1, 2, 3, 4)] == sorted(positions.values())


def test_approval_hardcoded_false_in_2d() -> None:
    code = _code(_cells())
    cell_2d = dict(code)[_one(code, r"#\s*2d\)", "2d")]
    assert "'approved': False" in cell_2d, "2d must hard-code approval to False"
    assert "'approved': True" not in cell_2d, "2d must NOT enable training"


def test_gctx_download_only_in_2c_after_2b_guard() -> None:
    code = _code(_cells())
    i_2b = _one(code, r"#\s*2b\)", "2b")
    i_2c = _one(code, r"#\s*2c\)", "2c")
    by_idx = dict(code)

    # The heavy gctx download appears ONLY in 2c (no other code cell touches it).
    with_dl = [i for i, src in code if "download_gctx" in src]
    assert with_dl == [i_2c], f"download_gctx must live only in 2c, found in {with_dl}"
    assert i_2b < i_2c, "the 2b guard must precede the 2c heavy step"

    # The 2b guard reads min_overlap (MIN_OVERLAP) and raises before any download.
    assert "MIN_OVERLAP" in by_idx[i_2b] and "raise SystemExit" in by_idx[i_2b]
    # 2c re-asserts the same guard (defense in depth) before fetching.
    assert "MIN_OVERLAP" in by_idx[i_2c] and re.search(r"^\s*assert ", by_idx[i_2c], re.M)
    # min_overlap is sourced from the registry, not hardcoded.
    src_2a = dict(code)[_one(code, r"#\s*2a\)", "2a")]
    assert "load_thresholds" in src_2a and "min_overlap" in src_2a


def test_inkernel_import_preflight_after_install() -> None:
    code = _code(_cells())
    install_idx = [
        i for i, src in code if re.search(r"pip install.*-e\s+packages/endoscan_core", src)
    ]
    preflight = [i for i, src in code if "endoscan_core import OK" in src]
    assert install_idx, "no editable-install cell for endoscan_core"
    assert len(preflight) == 1, "expected exactly one in-kernel import-preflight cell"
    assert min(install_idx) < preflight[0], "import-preflight must be AFTER the install"
    pf_src = dict(code)[preflight[0]]
    assert re.search(r"^\s*import endoscan_core", pf_src, re.M), "preflight must import in-kernel"
    assert not re.search(r"^\s*!.*python.*-c\b", pf_src, re.M), "no `!python -c` subprocess check"


def test_token_safe_clone_no_unauthenticated() -> None:
    cells = _cells()
    clone_cells = [src for _, src in _code(cells) if "git clone" in src or "git', 'clone'" in src]
    assert clone_cells, "expected a clone cell"
    for src in clone_cells:
        assert "GIT_ASKPASS" in src, "clone must use the askpass/token mechanism"
    for _, _, src in cells:
        assert "!git clone https://github.com/Rirkella/endoscan-platform.git" not in src


def test_no_banned_hotfix_patterns_in_code_cells() -> None:
    # CODE-CELL SOURCE ONLY — markdown prose may legitimately discuss these terms.
    srcs = _code_sources(_cells())
    blob = "\n".join(srcs)
    # No signature-level parser surgery / monkeypatching of a retriever.
    assert "_parse_lincs" not in blob, "no _parse_lincs reference in the operator notebook"
    assert "monkeypatch" not in blob, "no monkeypatch in the operator notebook"
    assert "setattr(" not in blob, "no setattr() (retriever) hotfix in the operator notebook"
    # No compound_key rename hotfix — build_lincs_parquet writes compound_id from the start.
    assert "rename(" not in blob, "no rename() hotfix (compound_key->compound_id) cell"
    # The clean notebook never references the back-compat alias token at all.
    assert "compound_key" not in blob, "the clean notebook must not reference compound_key"
    # No toxcast/tox21 staged-file identifiers (CERAPP is the ER label source here).
    assert "toxcast" not in blob and "tox21" not in blob, "no toxcast/tox21 staged identifiers"
