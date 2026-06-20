# M3 training test fixtures (FLAGGED FOR HUMAN REVIEW)

Offline, test-only fixtures that exercise the M3 ER pipeline deterministically.
They do **not** weaken any production gate.

## `quality_gates_relaxed.yaml`
Relaxed thresholds **for tests only**, so the tiny offline dataset can reach the
gate-PASS → train path. The real `registry/data/quality_gates.yaml` and the
committed `pipelines/endpoints/ER/config.yaml` stay strict and unapproved; the
strict **gate-blocks-training** path is tested separately with the real thresholds.

## `er_pipeline.test.yaml`
A test-only `PipelineConfig`: relaxed gate + `approved: true`, pointing at the
weak-signal training dataset below. The committed pipeline config is unaffected.

## `datasets/`
A training-only dataset directory read by the `FixtureSourceAdapter`. The label
and mapping files (`toxcast/tox21/cerapp/compara/pubchem.csv`) are **copies of the
M2 `tests/fixtures/datasets/` files** (same structure → same overlap/counts, so it
passes the relaxed gate). The **`lincs.csv` differs on purpose**: its landmark-gene
values carry **no class signal** (each gene vector — high / low / mid — appears in
both the active and inactive classes). This makes the cross-validated AUROC land
well below the `validated_mvp` floor (0.75), so the end-to-end run deterministically
registers ER as **`experimental`** — proving the threshold/status path without
fabricating metrics. The M2 fixtures under `tests/fixtures/datasets/` are left
untouched.

> Reviewer note: this is a deliberate, documented stand-in. The real ER (a later
> issue) trains on real data and produces real metrics.
