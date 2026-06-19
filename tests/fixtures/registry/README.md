# Registry test fixtures — schema (FLAGGED FOR HUMAN REVIEW)

These are **dummy fixtures** for M1 registry round-trip and validation tests.
They contain no real models, data, metrics, or scientific claims. They are
git-tracked via the M0 `.gitignore` exception for `tests/fixtures/**`.

> **Reviewer note:** this document defines the shapes these fixtures invent.
> Please confirm the field names, ID types, and label encoding before they are
> relied on by later milestones.

## ID types
- `endpoint_id` (`DEMO_ER`) is an **uppercase symbolic endpoint token**, matching
  `^[A-Z][A-Z0-9_]*$`. It is deliberately **distinct from `ER`**, the real
  endpoint M3 will register under `models/ER/`, so the fixture can never be
  confused with a real model.
- These fixtures contain **no compound identifiers**. CID / InChIKey / SMILES /
  perturbagen IDs belong to the M2 *dataset* fixtures, not the registry index.

## Layout
```
tests/fixtures/registry/
├── README.md                     # this file
├── endpoint_entry.json           # a valid serialized EndpointEntry (status: candidate)
└── models/DEMO_ER/               # artifacts referenced by endpoint_entry.json
    ├── model.pkl                 # pickled stub (NOT a real model)
    ├── feature_schema.json
    ├── metrics.json
    ├── model_card.md
    └── dataset_card.md
```

During tests these artifacts are copied to `<tmp_path>/models/DEMO_ER/` and the
index is seeded at `<tmp_path>/registry/models/endpoints.json`, so paths in
`endpoint_entry.json` are written **relative to repo root** (e.g.
`models/DEMO_ER/model.pkl`). The real `registry/models/endpoints.json` is never
modified by tests.

## File schemas

### `endpoint_entry.json`
A serialized `endoscan_core.registry.EndpointEntry`. Fields:

| Field | Type | Notes |
|---|---|---|
| `endpoint_id` | str | uppercase token (`DEMO_ER`) |
| `biological_target` | str | human-readable target |
| `input_type` | `"transcriptomics"` | constrained literal (only allowed value) |
| `model_path` | str | repo-root-relative path to `model.pkl` |
| `feature_schema_path` | str | repo-root-relative path |
| `metrics_path` | str | repo-root-relative path |
| `explainer_path` | str \| null | `null` at M1 (no explainer) |
| `model_card_path` | str | repo-root-relative path |
| `dataset_card_path` | str | repo-root-relative path |
| `status` | enum str | one of the `EndpointStatus` values; `candidate` here |
| `version` | str | semver-ish string (`0.1.0`) |
| `created_at` | str | ISO-8601 datetime (`2026-06-19T00:00:00Z`) |
| `source_refs` | list[str] | **opaque** provenance tags at M1 (no `sources.yaml` until M2) |

### `models/DEMO_ER/model.pkl`
A **pickled stub** — a plain `dict`, NOT a real estimator. M1 only deserializes
it via `load_model` to prove the registry hands back the referenced artifact.
Contents:
```python
{
    "kind": "endoscan-demo-stub",
    "endpoint_id": "DEMO_ER",
    "n_features": 3,
    "features": ["GENE_A", "GENE_B", "GENE_C"],
}
```
Regenerate with `tests/fixtures/registry/make_model_stub.py`.

### `models/DEMO_ER/feature_schema.json`
```json
{ "features": ["GENE_A", "GENE_B", "GENE_C"], "schema_version": "0.1" }
```
`features` is a list of gene-symbol strings (transcriptomic features).

### `models/DEMO_ER/metrics.json`
Placeholder metrics — all numeric values are `null` (no fabricated performance,
per PROJECT_RULES.md §6.1). Real metrics arrive with M3 training.

### `models/DEMO_ER/model_card.md`, `dataset_card.md`
Minimal placeholder cards (stub content), present so the `validated_mvp`
artifact-existence gate has real files to check.
