# Status and Roadmap

## How to read status

Four independent columns prevent “implemented” from being mistaken for “validated.” **Implemented** means the code path exists in the consolidated tree. **Offline validated** means automated fixtures/tests exercise it without external services. **Live technically validated** means a bounded recorded source/provider smoke established transport/parser/artifact behavior. **Scientifically validated** means evidence and model suitability have completed expert validation; no current endpoint has that status.

## Canonical status matrix

| Component | Implemented | Offline validated | Live technically validated | Scientifically validated | Notes / next evidence |
|---|---:|---:|---:|---:|---|
| Analysis API and React workspace | Yes | Yes | Local browser gate completed historically | No | Current interactive browser evidence remains pending |
| Saved analyses, Projects, Library, Explore | Yes | Yes | Local real-stack restore gate completed historically | No | Capture a reviewed current screenshot package |
| Admin Console | Yes | Yes | Historical local real-stack gates | No | Auth/RBAC and current browser evidence remain pending |
| AR and ER serving registry/models | Yes | Yes | Local serving paths exercised | No | External dataset/model review and independent validation required |
| SQLite/Alembic durable workflow control plane | Yes | Yes | Local restart/use exercised | No | Production DB, backup, concurrency and operations pending |
| State machine, approvals, events, artifacts, budgets | Yes | Yes | Selected real-run traces exist | No | Current consolidated browser/admin evidence pending |
| Provider-neutral model harness and deterministic offline provider | Yes | Yes | OpenAI boundary exercised in historical bounded runs | No | Repeat controlled acceptance only when explicitly authorized |
| PubChem BioAssay provider | Yes | Yes | Yes, bounded selected operations | No | Endpoint-specific suitability remains a scientific review |
| Tox21 provider | Yes | Yes | Yes, bounded exact release operations | No | Validate intended endpoint/release and licensing per build |
| EPA ToxCast/invitroDB public-release provider | Yes | Yes | Yes, bounded release/archive path | No | Full endpoint-specific assembly remains pending |
| NCBI GEO provider | Yes | Yes | Yes, bounded selected series metadata | No | General discovery relevance and matrix assembly remain pending |
| LINCS L1000 metadata/streaming provider | Yes | Yes | No | No | Execute reviewed bounded live metadata acceptance before reliance |
| PubChem Compound identity provider | Yes | Yes | Yes, bounded sampled mappings | No | Scale and ambiguity policy require build-specific review |
| NCBI supporting-metadata provider | Yes | Yes | No | No | Bounded live acceptance pending |
| Semantics-v2 discovery planning/execution/materialization | Yes | Yes | Partial historical attempts | No | Complete end-to-end live discovery acceptance pending |
| Candidate hydration and deterministic batching | Yes | Yes | Partial historical evidence | No | Verify on fresh authorized multi-source build |
| Combination coverage and joinability reporting | Yes | Yes | No complete broad-TR acceptance | No | Demonstrate verified activity/transcriptomics bridge live |
| Strategy generation and modality aggregation policy | Yes | Yes | No | No | Human-review acceptance with real evidence pending |
| Versioned `AssemblyRecipe` | Yes | Yes | No | No | Approval against a complete live inventory pending |
| Deterministic dataset assembly and preview | Yes | Yes | No | No | Approved live inventory and recipe required |
| Heavy-data jobs (GCTX, identity, coverage, fusion, Explore) | Yes | Yes | Selected source mechanics only | No | Bounded integration on approved real artifacts pending |
| Explore/UMAP artifact generation and serving | Yes | Yes with fixtures | Local serving paths only | No | Current endpoint artifact coverage and browser evidence pending |
| Benchmarking and compound-grouped evaluation | Yes | Yes via prepared fixtures | No end-to-end live endpoint | No | Independent benchmark design and real dataset required |
| Model review gate and model-card/validation artifacts | Yes | Yes via prepared fixtures | No | No | Expert review workflow on real benchmark pending |
| Endpoint/model registry publication | Yes | Yes via prepared fixtures and existing serving entries | Local serving verification | No | Governance and release operations pending |
| Offline TR and DNA-damage lifecycle demos | Yes | Yes | Not applicable | No | Prepared fixtures; not scientific discoveries |
| Broad thyroid-receptor endpoint build | Foundations only | Representative fixtures only | Incomplete/failed historical attempts | No | Do not claim a successful joinable live dataset |
| Thyroid-receptor scientific model validation | No final real model | No—fixtures are architecture-only | No | No | Real dataset, benchmark, expert and independent validation required |
| CI, command surface, docs and repository hygiene | Yes | Yes | Not applicable | Not applicable | Final repository/offline audit complete; screenshot package pending |
| Production auth, RBAC, multi-user deployment, monitoring | No | No | No | No | Required before production exposure |

Historical live-smoke transcripts remain available through Git history. Technical smoke evidence is not product or scientific certification.

## What is ready now

- Local internal demonstration of chemical analysis and endpoint administration.
- Fully offline engineering validation of two complete endpoint lifecycles.
- Extension of typed providers, scientific contracts, deterministic jobs, and review gates.
- Review of artifacts, provenance, budgets, errors, coverage, strategies, and benchmark lineage.

## What is not ready

- Clinical, regulatory, environmental, or automated safety decisions.
- Claims that broad thyroid-receptor discovery produced a complete joinable training table.
- Unattended live agents or unrestricted source access.
- Public or multi-tenant deployment.
- Scientific endorsement of current AR/ER models.

## Roadmap

### Browser evidence and demonstrator release

Complete current real-browser acceptance, accessibility/responsive review, and truthful screenshot capture. Record immutable release evidence without changing scientific semantics; the consolidated offline gate and documentation cross-check are complete.

### Provider and live-workflow acceptance

Complete LINCS and NCBI supporting-metadata technical smokes. Execute one explicitly authorized, bounded multi-source discovery on a fresh reviewed scope; prove hydration, identifier bridge, modality-specific overlap, and an evidence-backed joinability outcome.

### Scientific validation

Approve source releases and licenses, label and aggregation policies, identity mappings, dataset assembly, leakage-resistant splits, benchmark design, calibration/applicability analysis, model cards, and independent validation for each endpoint.

### Production engineering

Add authentication/RBAC, PostgreSQL or equivalent, object storage, migrations/backup operations, deployment images, network egress policy, managed secrets, observability, rate limiting, privacy/retention controls, signed releases, and incident response.
