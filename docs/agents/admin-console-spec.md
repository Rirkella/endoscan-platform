# Admin console specification

The primary interaction is workflow control and scientific review, not chat. All mutations require an authenticated administrator and optimistic-lock version.

## `/admin/endpoints`

### Endpoint-build list

- Columns: endpoint/draft name, build ID, current state, current stage owner, pending approval, last event, age, creator and health.
- Filters: active, awaiting me, failed, paused, completed/cancelled; endpoint and creator search.
- Actions: Create endpoint, open build, resume recent draft. No bulk approval or bulk registration.
- Status distinguishes model-running, deterministic tool-running, waiting for human, paused, failed and completed.
- Empty state explains the governed workflow and links to existing registered endpoints.

### Create endpoint drawer/page

Fields: display name, proposed ID, biological definition, transcriptomic claim, excluded claims, organism, cell/context preferences, label concept, intended use and owner. Inline validation enforces a transcriptomics input and conservative claims. Save creates `DRAFT`; Start workflow is disabled until endpoint-definition approval exists.

## `/admin/endpoints/:buildId`

### Header/control bar

Shows build ID, endpoint definition version, state, version, current attempt, creator, timestamps and cached/live indicators. Contextual controls only: Start, Pause, Resume, Cancel, Retry failed stage. Buttons show guards and require a confirmation with reason. Cancel is destructive to execution but retains history/artifacts.

### Stage timeline

A vertical/compact timeline renders every state from draft through completion, with attempt status, responsible agent/tool/human, start/end, artifacts, approval and error. It exposes the current state and valid next actions. Clicking a stage filters the trace and artifacts.

### Approval card

Displays approval type, immutable proposal version/hash, requested action, agent recommendation, evidence, limitations, exact citations, affected artifacts and prior decisions. Actions: Approve, Reject, Request revision, Choose alternative, Cancel. A comment is required for all except simple approval; high-risk approvals require reauthentication. The UI never shows an approval checkbox beside a failed gate.

### Dataset candidate comparison

Rows are candidates; columns include verified accession, title/source/version, licence, retrieval date/checksum, endpoint relevance, assay/label semantics, organism/cell context, matched controls, approximate sample information clearly labelled metadata-derived, identifier fields, access method, limitations and recommendation. Numeric audits appear only after deterministic inspection. Users can select an alternative and attach rationale.

### Agent-run trace

Shows a concise reasoning summary, not hidden chain-of-thought: goal, inputs, cited findings, recommendation, uncertainties and escalation. A separate event table shows provider/model/prompt version, turns, tool calls, arguments with secrets redacted, result artifacts, status, latency, usage and cost. Raw external text is visibly untrusted.

### Identity-resolution conflicts

Shows input identifiers, candidate canonical identities, evidence source, mapping confidence, affected rows and downstream impact. The reviewer may choose a supported identity, exclude the rows, request more evidence or cancel. No default selection for ambiguity.

### Model evaluation summary

Shows data snapshot hash, compounds/classes, split strategy, leakage result, candidate scorecard, selected model, nested/holdout metrics, bootstrap CIs, calibration/Brier, per-fold support, model stability, configured floors/ceilings, proposed status and limitations. The acceptance control remains separate from registration.

### Artifact browser

Lists kind, logical name, version, producer/tool version, checksum, size, created time, approval bindings and supersession. Safe text/JSON/CSV previews; binaries are metadata-only. Download requires authorization and records an audit event. No arbitrary filesystem paths.

### Registration confirmation

Summarizes the exact `EndpointEntry`, artifact set hash, cards, status, source refs, serving impact and rollback reference. Requires a distinct `endpoint_registration` approval. On success show registry commit/reference, `/endpoints` verification, model-load smoke and Model Library link.

## API surface

Suggested thin routes (domain service owns logic):

```text
GET    /admin/endpoint-builds
POST   /admin/endpoint-builds
GET    /admin/endpoint-builds/{id}
POST   /admin/endpoint-builds/{id}/start
POST   /admin/endpoint-builds/{id}/pause
POST   /admin/endpoint-builds/{id}/resume
POST   /admin/endpoint-builds/{id}/cancel
POST   /admin/endpoint-builds/{id}/retry
GET    /admin/endpoint-builds/{id}/timeline
GET    /admin/endpoint-builds/{id}/artifacts
GET    /admin/endpoint-builds/{id}/agent-runs
GET    /admin/endpoint-builds/{id}/approvals
POST   /admin/approvals/{id}/decisions
POST   /admin/endpoint-builds/{id}/register
GET    /admin/events/stream?workflow_id=...
```

Every command includes `expected_version` and `Idempotency-Key`; mutations return the new workflow snapshot. SSE is sufficient for the demonstrator; reconnect uses the last event sequence. Backend routes never call `endoscan_core` directly from request handlers for long work; they enqueue a typed activity and return 202.

## States and errors

- 400: schema or unsupported action shape.
- 401/403: unauthenticated/role or permission failure.
- 404: build/artifact/approval not found.
- 409: stale version, invalid transition, unmet guard or duplicate idempotency key with different content.
- 422: scientifically required structured input absent.
- 429: model/tool/workflow budget or rate limit.
- 503: provider/worker/source unavailable, with a safe retry suggestion.

The UI must preserve the workflow page on error, show request/event ID, and never optimistically display a transition before the server commits it.

## Accessibility and demo requirements

- State is conveyed by text/icon as well as color; approvals and destructive controls are keyboard accessible.
- Long traces and tables support progressive disclosure; key limitations are not collapsed by default at scientific/model approval.
- The demo has a seeded `demo` build selector that opens a precomputed, checksummed workflow while still allowing one live discovery run.
- No generic assistant bubble, anthropomorphic typing animation or fabricated progress. Progress is derived from persisted state/events.
