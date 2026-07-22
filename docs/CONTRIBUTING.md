# Contributing to EndoScan

EndoScan combines software, data engineering, and scientific policy. A change is complete only when its behavior, evidence boundary, tests, documentation, and limitations agree.

## Start here

1. Read [Architecture](ARCHITECTURE.md), [Endpoint Building Workflow](ENDPOINT_BUILDING_WORKFLOW.md), and [Scientific Limitations](SCIENTIFIC_LIMITATIONS.md).
2. Install the locked workspace using [Development](DEVELOPMENT.md).
3. Make the smallest coherent change on a dedicated branch.
4. Add deterministic offline tests and update canonical docs/status when behavior changes.
5. Run `uv run python scripts/project.py verify` and `git diff --check`.

## Change rules

- Keep measured transcriptomic response as the primary input to current endpoint models.
  A future structure-to-response model must remain an explicitly lower-evidence upstream
  component and must not become a direct structure-to-risk shortcut.
- Keep scientific/domain logic in `endoscan_core`, durable orchestration and provider
  integration in `endoscan_workflows`, and HTTP composition in the thin API service.
- Use the reviewed source registry and typed provider boundary; never turn a model-supplied
  URL into an unreviewed source request.
- Preserve source modality, raw observations, provenance, and artifact lineage.
- Put aggregation, labels, conflict handling, and exclusions in versioned reviewed policies.
- Split datasets by compound identity rather than rows, preserve assay/conflict evidence,
  and record applicability and uncertainty limitations.
- Keep model outputs behind strict contracts and deterministic validation.
- Keep URLs, hosts, request limits, and parsers in reviewed provider definitions.
- Treat zero candidates and missing prerequisites as typed outcomes.
- Add migrations for durable schema changes; never rewrite published migration history.
- Do not make tests depend on live OpenAI or scientific-source calls.
- Do not commit runtime databases, provider caches, downloads, matrices, demo output, secrets, credentials, or machine-local paths.
- Do not present prepared fixtures or technical smoke results as scientific validation.
- Do not weaken thresholds, bypass approvals, fabricate data, or relax assertions to obtain
  a desired result. EndoScan outputs research signals, not regulatory, clinical, diagnostic,
  or general safety verdicts.

## Pull requests

Describe the problem, implementation boundary, changed contracts/state, migration impact, tests, offline/live evidence, scientific implications, security implications, limitations, and rollback. Identify which [status-matrix](STATUS_AND_ROADMAP.md#canonical-status-matrix) cells change and why. Live operations require separate explicit authorization and should be reported with budgets, counts, safe diagnostics, artifact hashes, and absence of unintended retries.

## Architecture decisions

Add an ADR when changing a durable storage boundary, workflow state/gate, provider trust boundary, artifact/provenance rule, endpoint-label semantics, registry publication contract, or deployment model. Follow [the ADR index](decisions/README.md).

## Code review checklist

- Typed inputs/outputs and backward compatibility are explicit.
- State transitions and approvals cannot be bypassed.
- Errors preserve safe diagnostic fields and redact secrets.
- Time, size, pagination, batching, cost, and retry bounds are tested.
- Raw and derived artifacts remain distinguishable and immutable.
- Scientific assumptions, exclusions, missingness, and conflicts are inspectable.
- Frontend behavior has tests and accessible loading/error/empty states.
- Canonical docs, command surface, CI, and status claims remain true.

## Licensing

The repository is proprietary and confidential. Contribution or repository access does not grant permission to copy, distribute, or reuse the software beyond the owner's authorization. See [LICENSE](../LICENSE).

AI-assisted contribution practices are documented separately in
[AI-assisted development](AI_ASSISTED_DEVELOPMENT.md); private assistant-control files and
temporary prompts are not repository governance.
