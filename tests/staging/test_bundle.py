"""Review-bundle assembly: copy present artifacts, report missing ones."""

from __future__ import annotations

import bundle  # noqa: E402 — resolved via tests/staging/conftest.py sys.path


def test_assemble_review_bundle_copies_present_and_reports_missing(tmp_path) -> None:
    src = tmp_path / "repo"
    (src / "models" / "ER").mkdir(parents=True)
    (src / "models" / "ER" / "metrics.json").write_text("{}", encoding="utf-8")
    (src / "models" / "ER" / "model.pkl.dvc").write_text(
        "outs:\n- path: model.pkl\n", encoding="utf-8"
    )
    out = tmp_path / "bundle"

    copied, missing = bundle.assemble_review_bundle(
        src,
        out,
        ["models/ER/metrics.json", "models/ER/model.pkl.dvc", "models/ER/model_card.md"],
    )

    assert copied == ["models/ER/metrics.json", "models/ER/model.pkl.dvc"]
    assert missing == ["models/ER/model_card.md"]
    assert (out / "models" / "ER" / "metrics.json").read_text(encoding="utf-8") == "{}"
    assert (out / "models" / "ER" / "model.pkl.dvc").is_file()
