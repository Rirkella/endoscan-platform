"""dataset_quality_report: checks computed + dataset card written."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from endoscan_core.datasets import dataset_quality_report


def _report(tmp_path: Path, er_artifacts: SimpleNamespace, write_card: bool = True):
    overlap = er_artifacts.overlap
    return dataset_quality_report(
        "ER",
        er_artifacts.table,
        er_artifacts.labels,
        er_artifacts.signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        tmp_path,
        write_card=write_card,
    )


def test_report_metrics(tmp_path: Path, er_artifacts: SimpleNamespace) -> None:
    report = _report(tmp_path, er_artifacts)
    assert report.n_overlap == 7
    assert report.per_class_compound_counts == {1: 3, 0: 3}
    assert abs(report.duplicate_signature_rate - 0.125) < 1e-9
    assert report.metadata_coverage == 1.0
    assert report.n_label_conflicts == 1
    assert report.compound_level_leakage is False
    assert report.split_feasible is True


def test_dataset_card_written_and_rendered(tmp_path: Path, er_artifacts: SimpleNamespace) -> None:
    report = _report(tmp_path, er_artifacts)
    card = Path(report.dataset_card_path)
    assert card.exists()
    text = card.read_text(encoding="utf-8")
    assert "Dataset Card" in text
    assert "{{" not in text  # all placeholders filled
