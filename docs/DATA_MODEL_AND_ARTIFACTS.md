# Data Model and Artifacts

## Storage split

EndoScan separates control-plane records from scientific payloads.

- **SQLite/Alembic** stores workflows, builds, steps, state/version, approvals, agent runs, budgets, tool calls, diagnostics, errors, immutable events, cache references, and artifact metadata.
- **Content-addressed storage** stores raw provider responses, normalized scientific observations, plans, reports, datasets, benchmark outputs, and registry payloads. SHA-256 identity makes mutation detectable.
- **Endpoint registry and model directories** hold only reviewed serving manifests and the small committed serving artifacts explicitly allowed by repository policy.
- **Heavy-data local storage** is runtime state for source archives, matrices, and job outputs and is never committed.

## Durable entities

| Entity | Role |
|---|---|
| Build/workflow | Endpoint goal, state, optimistic version, timestamps, terminal status |
| Step | Bounded unit of work and its input/output/error references |
| Approval | Version-bound human decision at a named gate |
| Agent run | Provider/mode/model, budgets, turns, usage, cost, diagnostics |
| Tool call | Typed arguments, normalized arguments, status, latency, output reference |
| Immutable event | Append-only explanation of state-changing facts |
| Artifact | ID, media type, size, SHA-256, producer, lineage, storage locator |
| Cache entry | Boundary fingerprint and reference to an existing immutable artifact |
| Error | Safe normalized type/code/message and provider diagnostic fields |

Alembic migrations under `packages/endoscan_workflows/endoscan_workflows/migrations` are the schema history. Repository classes isolate SQL operations; Pydantic contracts isolate serialized meaning.

## Artifact classes

Important artifacts include specifications and discovery scopes; provider capability snapshots; discovery plans and ledgers; raw source responses; candidate and hydrated observation sets; coverage and joinability reports; strategy proposals; approved assembly recipes; modality aggregation policies; dataset previews and manifests; identifier bridges; benchmark plans/results; model binaries; registry manifests; and limitations reports.

Raw source bytes are immutable. Normalized artifacts reference their raw inputs. Derived artifacts record producing code/contract versions and parent hashes. A cache hit points to the same stored artifact; it does not manufacture a second observation.

## Scientific row principles

Activity observations retain source/assay identifier, target, modality, endpoint/measurement, value or call, compound identifier, and provenance. Transcriptomic observations retain profile/matrix locator, compound identifier, biological model, dose, duration, processing level, and provenance. Identifier bridges preserve all source IDs, canonical mapping, mapping method/status, and source evidence.

Original records are never overwritten by derived labels. Aggregated endpoint rows reference the approved policy and contributing records. Missingness, conflicts, exclusions, and rejected mappings remain inspectable.

## Fingerprints and versioning

Boundary fingerprints cover normalized typed inputs, tool/provider version, source release or locator, and relevant policy. Dataset fingerprints cover ordered inputs and assembly policy. Registry versions pin model, features, schema, dataset/benchmark lineage, and limitations. Any semantic change creates a new artifact or version; it does not edit history.

## Local paths and cleanup

Default workflow databases and artifacts live below ignored `.endoscan/`; job payloads live below ignored `.endoscan-data/`; offline demos live below ignored `.builds/`. These are disposable local runtime roots unless an operator deliberately archives them. They must not be committed, used as portable documentation links, or treated as a backup.

## Backup and portability

A reproducible export needs database records plus every referenced artifact and registry payload, verified by hashes. Copying SQLite alone is insufficient. Productionizing this architecture requires transactional backup across a server-grade database and object store, retention policy, encryption, access control, and restore drills.
