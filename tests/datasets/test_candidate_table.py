"""candidate_table_builder: X/y/metadata shapes + compound-level split plan."""

from __future__ import annotations

from types import SimpleNamespace


def test_table_shapes(er_artifacts: SimpleNamespace) -> None:
    table = er_artifacts.table
    assert list(table.X.columns) == table.feature_names
    assert len(table.X) == 8  # C1 has 3 signatures; C2..C6 one each
    assert len(table.y) == len(table.X) == len(table.metadata)
    assert set(table.y.unique()) == {0, 1}


def test_conflicted_compound_excluded(er_artifacts: SimpleNamespace) -> None:
    assert er_artifacts.table.excluded_conflicts == ["GGGGGGGGGGGGGG-GGGGGGGGGG-G"]
    keys = set(er_artifacts.table.metadata["compound_key"])
    assert "GGGGGGGGGGGGGG-GGGGGGGGGG-G" not in keys


def test_split_plan_is_compound_level(er_artifacts: SimpleNamespace) -> None:
    plan = er_artifacts.table.split_plan
    assert plan.strategy == "compound_level"
    assert len(plan.groups) == 6  # usable compounds C1..C6
    assert "split_group" in er_artifacts.table.metadata.columns
    # every fold's compounds are disjoint
    seen: set[str] = set()
    for keys in plan.folds.values():
        assert not (seen & set(keys))
        seen.update(keys)
