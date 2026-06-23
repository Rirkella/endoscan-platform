"""Compound-identity helpers: InChI->InChIKey seam, messy-CASRN normalization, derive.

CI has NO RDKit, so the InChI->key seam is exercised by monkeypatching
``identity.inchi_to_inchikey`` with known InChI->InChIKey pairs. The CASRN
normalization + check-digit validation and the unresolved-counting behaviour are
tested directly.
"""

from __future__ import annotations

import identity  # noqa: E402 — resolved via tests/staging/conftest.py sys.path

# A couple of real InChI -> InChIKey pairs (used by the monkeypatched seam).
_BPA_INCHI = "InChI=1S/C15H16O2/c1-15(2,11-3-7-13(16)8-4-11)12-5-9-14(17)10-6-12/h3-10,16-17H,1-2H3"
_BPA_KEY = "IISBACLAFKSPIT-UHFFFAOYSA-N"


def test_is_valid_casrn_checks_format_and_check_digit() -> None:
    assert identity.is_valid_casrn("50-00-0")  # formaldehyde, valid check digit
    assert identity.is_valid_casrn("80-05-7")  # bisphenol A, valid check digit
    assert not identity.is_valid_casrn("50-00-1")  # wrong check digit
    assert not identity.is_valid_casrn("not-a-casrn")
    assert not identity.is_valid_casrn("")
    assert not identity.is_valid_casrn(None)


def test_normalize_casrn_candidates_strips_suffix_splits_and_validates() -> None:
    # Trailing "(1)" count suffix is stripped; the valid CASRN survives.
    assert identity.normalize_casrn_candidates("80-05-7 (1)") == ["80-05-7"]
    # Comma/semicolon lists split into multiple candidates (order preserved, deduped).
    assert identity.normalize_casrn_candidates("50-00-0, 80-05-7; 50-00-0") == [
        "50-00-0",
        "80-05-7",
    ]
    # Leading-zero padding is stripped before validation.
    assert identity.normalize_casrn_candidates("0000050-00-0") == ["50-00-0"]
    # A bad check digit is rejected -> dropped (not silently mapped later).
    assert identity.normalize_casrn_candidates("50-00-1") == []
    assert identity.normalize_casrn_candidates("") == []
    assert identity.normalize_casrn_candidates(None) == []


def _labels() -> list[dict]:
    return [
        {"casrn": "80-05-7", "inchi_code": _BPA_INCHI, "label": 1},  # derived from InChI
        {"casrn": "50-00-0", "inchi_code": "", "label": 0},  # no InChI -> CASRN fallback
        {"casrn": "garbage", "inchi_code": None, "label": 1},  # neither -> unresolved
    ]


def test_derive_inchikeys_primary_inchi_then_casrn_then_unresolved(monkeypatch) -> None:
    # Monkeypatch the RDKit seam so CI needs no RDKit: only the BPA InChI resolves.
    monkeypatch.setattr(
        identity, "inchi_to_inchikey", lambda inchi: _BPA_KEY if inchi == _BPA_INCHI else None
    )
    # CASRN fallback resolver: formaldehyde maps; everything else misses.
    resolver = {"50-00-0": "wsfsskykwvqejq-uhffffaosa-n"}.get  # lower-case on purpose

    rows, stats = identity.derive_inchikeys(_labels(), casrn_resolver=resolver)
    by_casrn = {r["casrn"]: r for r in rows}

    # Row 1: derived from InChI, normalized to the full upper-case key.
    assert by_casrn["80-05-7"]["inchikey"] == _BPA_KEY
    assert by_casrn["80-05-7"]["inchikey_source"] == "inchi"
    # Row 2: no InChI -> CASRN fallback, resolver output normalized (upper-cased).
    assert by_casrn["50-00-0"]["inchikey"] == "WSFSSKYKWVQEJQ-UHFFFFAOSA-N"
    assert by_casrn["50-00-0"]["inchikey_source"] == "casrn_fallback"
    # Row 3: neither path resolves -> kept, counted as unresolved, NOT dropped.
    assert by_casrn["garbage"]["inchikey"] is None
    assert by_casrn["garbage"]["inchikey_source"] is None

    assert len(rows) == 3  # no row dropped
    assert stats == {
        "n_labels": 3,
        "derived_from_inchi": 1,
        "derived_from_casrn_fallback": 1,
        "unresolved": 1,
    }


def test_derive_inchikeys_without_resolver_counts_unresolved(monkeypatch) -> None:
    # With no fallback resolver, rows lacking a derivable InChI are unresolved (not errors).
    monkeypatch.setattr(identity, "inchi_to_inchikey", lambda inchi: None)
    rows, stats = identity.derive_inchikeys(_labels())
    assert all(r["inchikey"] is None for r in rows)
    assert stats["unresolved"] == 3 and stats["derived_from_casrn_fallback"] == 0
