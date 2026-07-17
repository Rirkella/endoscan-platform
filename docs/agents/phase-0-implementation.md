# Phase 0 agent-foundation implementation

## Scope and outcome

Phase 0 proves durable, reviewable orchestration and explicit human control. It does not prove
scientific discovery or autonomous model development. The existing deterministic ER/AR serving
and analysis paths remain unchanged. The administrative path is isolated under `/admin`, uses a
deterministic fake provider, and cannot publish the production endpoint registry.

The implementation follows the architecture, state machine, tool inventory, Admin Console, and
threat model in this directory. The bounded deviations are listed at the end of this document.

## Runtime architecture

`packages/endoscan_workflows` is the Phase-0 domain package. It contains:

- Pydantic contracts for workflow state, mutations, approvals, artifacts, provider calls, tools,
  traces, usage, and normalized errors;
- SQLAlchemy persistence and programmatic Alembic migration support;
- the guarded workflow state machine and transactional service;
- a content-addressed local artifact-store implementation;
- the provider-neutral harness and deterministic `FakeAgentProvider`;
- the typed tool registry and prepared dataset-discovery simulation.

`services/api/endoscan_api/admin` exposes the local-development administrative API.
`apps/web` supplies the workflow-focused build list and build detail screens. The browser never
acts as the authoritative workflow store.

## Database and migration design

The workflow database is SQLite with foreign-key enforcement, WAL journaling, a 30-second busy
timeout, and transactional SQLAlchemy sessions. Alembic revision `0001_phase0_foundation`
creates these tables:

- `endpoint_builds`: durable workflow identity, state, preserved pause/failure stage, lifecycle
  timestamps, version, and create-idempotency data;
- `workflow_steps`: bounded activity attempts, state, idempotency key, timestamps, and error link;
- `workflow_events`: ordered, hash-chained mutation history;
- `approvals`: immutable proposals and immutable decided records;
- `artifacts`: content hash, safe storage key, type, MIME, provenance, and producer step;
- `agent_runs`: provider/model/instruction identity, input hash, bounded usage, output, trace, and
  terminal status;
- `tool_calls`: logical call identity, validated input/output, timing, status, and replay link;
- `human_decisions`: append-only reviewer decisions;
- `workflow_errors`: sanitized, typed, retryable or terminal failures.

All stored structured JSON is schema-versioned. Read paths validate schema versions and typed
contracts before returning domain objects. Event and human-decision update/delete triggers make
history append-only; decided approval rows cannot be rewritten. Mutation services write the
domain change and its event in the same transaction.

IDs use deterministic UUID5-based public identifiers derived from stable workflow inputs and
idempotency keys. Timestamps are normalized UTC ISO-8601 values. Version-checked updates provide
optimistic concurrency control.

## State machine, transitions, and idempotency

The service loads and validates `workflow-state-machine.json` as its canonical transition map.
It supports DRAFT, DISCOVERING_DATA, all approval and scientific stages through REGISTERING and
COMPLETED, plus FAILED, PAUSED, and CANCELLED.

Every transition records its initiator, previous and next state, idempotency key, payload, event
sequence, and event hash. A repeated idempotency key with the same operation returns the prior
result; reuse with conflicting input is rejected. Stale expected versions fail before mutation.
Invalid transitions and missing guards roll back without an event.

Pause records the underlying stage. Resume restores that exact stage. Cancel is terminal. A
retry addresses the failed step and returns to the preserved failed stage. Completed logical
tool calls are replayed from their validated result rather than executed again.

## Approvals and human decisions

The approval contract supports endpoint definition, dataset selection, label rules, identity
conflict, training authorization, model acceptance, and registry publication. Each proposal is
bound to its workflow and stage, proposed decision, evidence, source references, limitations,
artifact hashes, recommendation, and requested reviewer action.

Proposal hashes cover that bound content. A reviewer must echo the current artifact hashes;
approval is rejected if the evidence changed. Approve, reject, request revision, choose
alternative, and allowed cancellation decisions are explicit typed values. A revision creates a
new proposal and preserves the old decision. Human-decision events and records are immutable.

In the demonstration, approval of the prepared dataset proposal advances to CURATING_DATA.
Reject and request-revision remain auditable; revision reruns the prepared comparison while
replaying completed idempotent inspection tools.

## Content-addressed artifact store

`LocalArtifactStore` stores immutable blobs by SHA-256 under
`<configured-root>/sha256/<prefix>/<digest>`. Callers provide bytes or typed JSON, never a trusted
filesystem destination. The store enforces a two-megabyte default limit, an allowlist of MIME
types, bounded generated paths, atomic writes, database metadata, and SHA-256 verification on
read. It supports put bytes, put JSON, get, verify, list for build, and attach to step. The
interface is intentionally replaceable by a future object-store adapter.

## Provider-neutral harness

`AgentProvider` accepts versioned instructions, model configuration, an output schema, available
tool definitions, typed context, turn/time/token/cost bounds, a trace callback, and interruption
context. It returns normalized output, usage, trace information, and errors. `ProviderRegistry`
selects configured providers; Phase 0 registers only `fake`.

`FakeAgentProvider` provides deterministic scripts for valid structured output, tool requests,
approval interruption, timeout, malformed output, provider failure, and transient failure. Its
usage is deterministic and has zero estimated provider cost. No OpenAI or Anthropic client is
present and no live model request can occur.

The harness, rather than the provider, invokes tools. It enforces maximum turns, tool calls,
wall-clock timeout, tokens, estimated cost, output-schema validation, one bounded schema-repair
attempt, normalized provider retries, allowlisted tools, permission scope, stage eligibility,
and terminal failure. It records provider/model/instruction version, input hash, attempts, tool
I/O, traces, validation failures, duration, usage, and final status. Secret-like fields are
redacted. There is no shell tool, URL-fetch tool, arbitrary filesystem tool, or registry-write
tool.

## Phase-0 tools and discovery simulation

The production Phase-0 registry contains four bounded tools:

| Tool | Purpose | Side effects |
| --- | --- | --- |
| `inspect_endpoint_registry` | Read a bounded summary and digest of the real endpoint registry | Read only |
| `list_known_source_adapters` | List configured source-adapter capabilities | Read only |
| `summarize_existing_endpoint_pipeline` | Summarize the deterministic repository pipeline | Read only |
| `create_dataset_candidate_artifact` | Produce a typed prepared-candidate payload for the harness | Workflow artifact only |

Tool definitions include typed input/output, permissions, side-effect and idempotency classes,
timeout, allowed stages, and implementation version. Test-only tools cover success, validation
failure, timeout, prohibited use, and idempotent retry.

The first functional path is intentionally narrow:

1. an administrator creates an Oxidative stress draft;
2. start advances it to DISCOVERING_DATA;
3. the fake Dataset Discovery Agent requests the four allowed tools;
4. two or more prepared candidate fixtures are compared;
5. the immutable comparison artifact is stored;
6. a dataset-selection approval is created;
7. the workflow stops at AWAITING_DATASET_APPROVAL until a human decides.

The API and UI label this path **Prepared deterministic agent simulation** and state that the
candidates are not live scientific findings.

## Admin API and console

The `/admin` API provides capability status plus create/list/get/start/pause/resume/cancel/retry,
steps, timeline, artifacts and safe preview, approvals and decisions, agent runs and traces,
errors, and a development-only controlled failure. Mutation requests use idempotency keys and
expected workflow versions.

Routes fail closed unless `ENDOSCAN_ADMIN_MODE=development`. They additionally require
`X-EndoScan-Admin: local-development`, validate resource IDs, sanitize errors, and apply a local
mutation rate limit. This marker is not production authentication; deployments must leave the
routes disabled until Phase 1 authorization exists.

The Admin Console provides:

- `/admin/endpoints`: persisted build list, stage, status, progress, pending approval, create,
  refresh, and open actions;
- `/admin/endpoints/:buildId`: workflow controls, exact current stage, immutable timeline,
  pending approval and reviewer comment, candidate comparison, agent/tool trace, artifacts,
  errors, controlled failure, and retry.

Layouts collapse for narrow viewports and unknown future stage strings render without a fixed
ER/AR assumption. Chat is not the primary interface.

## Restart recovery and observability

Startup migrates the database and reconciles interrupted running steps. An interrupted running
step becomes an explicit retryable failure rather than remaining falsely active. Pending
approvals, artifacts, traces, and completed logical tool calls survive restart. Recovery does not
rerun completed tools.

Structured logs cover creation, transitions, agent-run and tool-call start/end, approvals,
artifacts, retries, failures, and recovery with the relevant workflow, step, run, call, duration,
event, and status identifiers. Artifact content, credentials, authorization values, and raw
sensitive inputs are excluded. `/health` reports workflow-database, artifact-store, configured
provider, and admin-development capabilities.

## Security boundary

Phase-0 controls include:

- no shell, arbitrary network fetch, arbitrary path, or registry-publication capability;
- strict Pydantic contracts and stored schema-version validation;
- stage-bound, permission-bound tool allowlists;
- bounded workflow agent turns, calls, time, tokens, cost, retries, artifact size, and preview;
- SHA-256 evidence binding and artifact integrity checks;
- optimistic concurrency, transaction rollback, immutable history, and sanitized API failures;
- development admin routes disabled by default and protected by an explicit local marker;
- secret redaction in persisted traces and structured logs.

The local marker and in-process rate limiter are defense in depth for development, not a
substitute for production identity, authorization, tenancy, audit export, or network policy.

## Run locally

Use the repository environment and install/sync workspace dependencies, then set:

```text
ENDOSCAN_ADMIN_MODE=development
ENDOSCAN_WORKFLOW_DB=.endoscan/workflows.db
ENDOSCAN_ARTIFACT_ROOT=.endoscan/artifacts
```

Start the FastAPI service and Vite application using the existing repository commands. Open
`/admin/endpoints`. The web client supplies the explicit local-development marker; it must only
be used against a local development server.

## Test and validate

The focused suites are:

```text
python -m pytest packages/endoscan_workflows/tests services/api/tests/test_admin_phase0.py
cd apps/web && npm test -- --run src/test/adminPhase0.test.tsx
cd apps/web && npm run typecheck && npm run build
```

The workflow tests use isolated temporary databases and artifact roots. They cover migration,
WAL, foreign keys, optimistic/concurrent transitions, rollback, immutable history, approval hash
binding, artifact integrity/limits/path safety, restart recovery, harness bounds, typed tool I/O,
provider failure/retry, trace/usage persistence, deterministic replay, authorization, API flow,
and registry immutability.

## Intentionally deferred to Phase 1 or later

- real OpenAI and Anthropic adapters or any live LLM call;
- live GEO, PubMed, or other scientific dataset discovery;
- RAG, embeddings, or a vector database;
- real oxidative-stress dataset construction and heavy training;
- production authentication, authorization, tenancy, and distributed rate limiting;
- distributed workers or Temporal;
- automatic or production endpoint-registry publication.

## Documented deviations

1. The audit separates conceptual workflow and agent packages. Phase 0 consolidates them in
   `endoscan_workflows` to keep one small dependency graph and transaction boundary. Public
   provider, tool, artifact, and service interfaces remain separable for Phase 1.
2. Explicit administrative draft creation is treated as the endpoint-definition approval for
   the demonstration path. The endpoint-definition artifact and event remain durable; a distinct
   pre-draft approval screen is deferred.
3. Discovery executes synchronously in the API process because the fake run is deterministic,
   bounded, and lightweight. Restart recovery is implemented, but distributed workers are
   explicitly deferred.
4. The initial Alembic revision creates the complete Phase-0 SQLAlchemy metadata and immutable
   triggers. Later changes must use additive explicit revisions rather than metadata recreation.
5. The controlled failure endpoint exists only inside the already development-gated admin
   namespace to make recovery and retry observable in acceptance testing.
