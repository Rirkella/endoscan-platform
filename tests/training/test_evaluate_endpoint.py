"""evaluate_endpoint metric correctness on small hand-built arrays."""

from __future__ import annotations

from endoscan_core.training.evaluate_endpoint import evaluate_endpoint


def test_perfect_separation() -> None:
    m = evaluate_endpoint([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9])
    assert m.auroc == 1.0
    assert m.f1 == 1.0
    assert m.confusion_matrix.tp == 2
    assert m.confusion_matrix.tn == 2
    assert m.confusion_matrix.fp == 0
    assert m.confusion_matrix.fn == 0
    assert m.n_samples == 4


def test_confusion_counts_at_threshold() -> None:
    # preds = [1,1,0,0] vs truth [0,1,0,1] -> tn=1, fp=1, fn=1, tp=1
    m = evaluate_endpoint([0, 1, 0, 1], [0.9, 0.9, 0.1, 0.1])
    cm = m.confusion_matrix
    assert (cm.tn, cm.fp, cm.fn, cm.tp) == (1, 1, 1, 1)
