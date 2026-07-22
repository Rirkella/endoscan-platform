# Target architecture for agent-assisted endpoint builds

> The general public-data training-dataset discovery architecture is specified in
> `training-dataset-discovery-and-assembly.md`. It supersedes fixed one-dataset or two-source
> interpretations while preserving all historical workflows.

## Architectural decision

Build a deterministic, database-backed orchestrator in the existing FastAPI/Python stack. Do **not** introduce Temporal, Prefect or Dagster for the first vertical slice.

Why:

- The workflow has fewer than twenty explicit states, one expected build at a time for the demo, and long human waits rather than high-throughput DAG scheduling.
- The repository has Pydantic models, a file state machine (`agent/build_state.py`) and a job runner (`endoscan_jobs/runner.py`) but no database or workflow infrastructure.
- SQLite plus an explicit transition service is the smallest dependency that survives restart, supports optimistic locking and is easy to ship locally.
- Temporal is the best later option if builds become concurrent, multi-day, distributed and operationally critical. Design activities and idempotency keys so they can be migrated. Prefect/Dagster are stronger for data orchestration than human approval semantics and would not remove the need for a product state model.

Use PostgreSQL when multiple admins/workers or a shared deployment are required. The schema and repository interfaces must remain database-neutral; SQLite is the demonstrator default.

## Component boundaries

| Component | Responsibility | Must not do |
|---|---|---|
| Workflow orchestrator | validate transitions, guards, checkpoints, retries, pause/resume, cancellation | reason about science or call a model directly |
| Agent harness | run one bounded agent with instructions, tools, schemas, budgets and trace | own workflow truth or publish an endpoint |
| Tool registry | expose typed, scoped functions and deterministic artifacts | provide shell, arbitrary paths or unvalidated network access |
| Model provider | normalize model/tool/usage behavior | execute tools or decide approvals |
| Human approval layer | create immutable review requests and decisions | mutate artifacts after approval |
| Worker/executor | run deterministic jobs with heartbeats and cancellation checks | choose scientific policy |
| Artifact store | immutable bytes/text, metadata and checksums | infer meaning or overwrite an approved version |
| Registry publisher | verify approved candidate and atomically publish | accept an agent recommendation as authorization |

Suggested modules for implementation (not created by this audit):

```text
services/api/endoscan_api/admin/
  routes.py schemas.py auth.py dependencies.py
packages/endoscan_workflows/
  domain.py transitions.py repositories.py service.py worker.py
packages/endoscan_agents/
  harness.py providers/base.py providers/openai.py providers/fake.py
  tools/registry.py tools/datasets.py agents/dataset_discovery.py
```

Keep numerical science in `endoscan_core`; network/heavy adapters belong in `endoscan_jobs`; the workflow and harness coordinate them.

## Durable state model

The canonical transition graph is `workflow-state-machine.json`. `workflow_runs.state` is current truth; `workflow_steps` and `workflow_events` are append-only history.

Rules:

- Every command supplies `workflow_id`, expected `version` and an idempotency key.
- A transaction locks the row (or checks `version` in SQLite), validates the transition and required artifact checksums, appends an event, then increments `version`.
- `PAUSED` stores `paused_from_state`; resume returns only to that state after its guards are rechecked.
- `FAILED` stores a retryable/non-retryable classification and `failed_from_state`; retry creates a new step attempt, never rewrites the failed attempt.
- Cancellation is cooperative. An active worker checks a cancellation token between bounded operations; completed immutable artifacts remain for audit but cannot be promoted.
- A stage success is idempotent when the same input artifact checksums, tool version, config version and idempotency key produce the same output-artifact reference.
- On restart, a reconciler marks stale `RUNNING` steps as interrupted, verifies artifacts, and either requeues the idempotent activity or returns the workflow to `FAILED` for review.
- Invalid transitions return HTTP 409 with the current state, allowed actions and unmet guards.

## Persistence model

Recommended tables:

| Table | Essential fields |
|---|---|
| `endpoint_builds` | id, slug, display_name, endpoint_definition_json, status, created_by, timestamps |
| `workflow_runs` | id, build_id, state, paused_from_state, failed_from_state, version, prompt_set_version, created/updated/completed |
| `workflow_steps` | id, workflow_id, state, attempt, status, input/output artifact IDs, idempotency_key, worker heartbeat, timing |
| `workflow_events` | sequence, workflow_id, actor_type/id, event_type, from/to state, payload, timestamp, hash |
| `agent_runs` | id, step_id, agent_name/version, provider/model, instructions hash, request/output, status, turns, usage, cost, timing |
| `tool_calls` | id, agent_run_id, tool/version, arguments, permission scope, result artifact, status, timing, error |
| `approvals` | typed object described below, proposal hash, immutable decision record |
| `artifacts` | id, workflow/step, kind, logical name, version, URI, sha256, bytes, media type, producer, created_at |
| `source_documents` | id, source/accession/title/URL/licence/version/retrieved_at/checksum/trust status |
| `retrieved_chunks` | document_id, section, exact span offsets/text, keyword/vector fields, injection flags |
| `prompts` | name, semantic version, template, schema hash, active_from/to |
| `model_usage` | agent_run_id, provider/model, input/output/cached tokens, estimated cost, request ID |
| `errors` | step/agent/tool association, normalized code, retryability, safe detail, internal trace reference |
| `endpoint_candidates` | build_id, endpoint entry JSON, artifact set hash, review status, published commit/ref |

Use Alembic migrations. SQLite runs in WAL mode with foreign keys enabled; one workflow command transaction at a time. Store large artifacts under a configured local root for MVP and S3/MinIO later. Artifact paths are generated from IDs, never caller-controlled. Names are immutable and content-addressed: `artifacts/{workflow_id}/{step}/{artifact_id}-{sha256[:12]}.{ext}`. Retain audit metadata indefinitely for the demonstrator; document a later policy for source documents and model traces. Never store API keys in workflow or serialized agent state.

## Human approval object

```text
Approval {
  id, workflow_id, stage,
  type: endpoint_definition|dataset_selection|label_rules|identity_conflict|
        training_authorization|model_acceptance|endpoint_registration,
  proposal_artifact_id, proposal_sha256,
  evidence_artifact_ids[], limitations[], source_reference_ids[],
  agent_recommendation, requested_action,
  status: pending|approved|rejected|revision_requested|alternative_selected|cancelled,
  reviewer_id, reviewer_roles[], decision_comment, selected_alternative_id,
  decided_at, created_at, affected_artifact_versions[], immutable_event_id
}
```

An approval is valid only for the exact proposal/artifact hashes. Approve advances when all guards pass. Reject returns to the explicitly configured revision state or cancels the candidate; it never silently chooses another dataset. Request revision creates a new proposal version and a new approval. Choose alternative records the chosen candidate and why. Cancel transitions the workflow to `CANCELLED`. Decisions are append-only; corrections are new superseding records.

## Agent harness

Each agent run is a single stage-level bounded operation.

- **System instructions:** versioned, name the stage, allowed evidence, prohibited actions, output schema, escalation rules, citation contract and the rule that external text is data, never instructions.
- **Typed tools:** Pydantic request/result objects; JSON Schema is sent to the provider. Tool results reference immutable artifacts for large data.
- **Structured output:** Pydantic validation with `extra="forbid"`; at most one repair attempt using validation errors. Invalid output then fails safely.
- **Budgets:** default maximum 8 turns, 12 tool calls, 30k input tokens, 6k output tokens, stage cost ceiling configured in cents, 120-second model timeout and per-tool timeouts. Discovery may have a separately approved higher network timeout.
- **Retries:** two transport retries with jitter; no automatic retry for schema failure after repair, policy refusal, budget exhaustion or non-idempotent tool failure.
- **Fallback:** return a typed `needs_human_review`/`unavailable` result with gathered sources. Never invent a candidate to make the workflow continue.
- **Permissions:** stage-specific capabilities, e.g. discovery gets allowlisted metadata search/read only; evaluation gets artifact reads; no agent gets shell, arbitrary filesystem write, secrets, training authorization or registry write.
- **Network:** tools own allowlisted hosts and redirect validation. The model itself receives no unrestricted browsing tool in the first slice.
- **Citations:** every factual recommendation links to a `source_document_id` and exact span. Unsupported statements fail output validation or are marked hypotheses.
- **Trace:** mirror provider request IDs, redacted prompts/outputs, model and prompt versions, tool calls, approvals, usage and timings into the local DB. Store sensitive-data capture off by default.
- **Injection protection:** source text is delimited and labelled untrusted; tool descriptions and system policy never come from retrieved text; suspicious instruction-like spans are flagged and excluded from decision summaries unless quoted as evidence of injection.
- **Replay:** deterministic tools replay from checksummed inputs. Agent runs replay against stored tool results for evaluation; a live model replay is a new run, not claimed bit-for-bit deterministic.

## Provider-neutral model interface

```text
AgentProvider.run(AgentRequest) -> AgentResult

AgentRequest:
  run_id, model_config, system_instructions, messages,
  tools[ToolDefinition], output_schema, max_turns,
  timeout_seconds, metadata, trace_context

AgentResult:
  status, structured_output, tool_calls[], finish_reason,
  provider_request_ids[], usage, estimated_cost, latency_ms,
  normalized_error, raw_reference (encrypted/retained by policy)
```

`ToolCall` contains provider-neutral `id`, `name`, validated JSON arguments and status. Streaming emits normalized events (`text_delta`, `tool_call_started`, `tool_arguments_delta`, `usage`, `completed`, `error`) but only validated structured output advances a workflow. Normalize rate limit, timeout, authentication, content refusal, context overflow, invalid tool call and provider unavailable errors.

Implement `FakeAgentProvider` first for transition/API/UI tests. Implement `OpenAIProvider` next using the Python OpenAI Agents SDK/Responses API. Keep provider model names in configuration and persist the exact resolved model on each run. A future `AnthropicProvider` maps its tool blocks, streaming and usage into the same contracts; provider-specific options live in an opaque, allowlisted config field and never leak into domain schemas.

### First provider recommendation

OpenAI first is the shortest implementation path because the Python Agents SDK provides Pydantic-derived function tools, managed agent loops, guardrails, sessions, structured outputs, human-in-the-loop interruption/resume and built-in traces. The SDK uses the Responses API by default. The workflow DB remains authoritative; SDK `RunState` may be stored as provider-specific continuation data, not as the workflow state machine.

Use a configurable current general reasoning model rather than embedding a permanent model ID in scientific artifacts. Start the demo profile with the current balanced cost/intelligence model and reserve the flagship reasoning model for difficult evaluation cases; lock the resolved ID in each trace and benchmark before changing it. Official references: [Agents SDK overview](https://openai.github.io/openai-agents-python/), [human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/), and [tracing](https://openai.github.io/openai-agents-python/tracing/).

This choice is not provider lock-in: tools, schemas, orchestration, approvals, traces and artifacts remain EndoScan-owned. Do not implement two production providers in the first slice.

## Retrieval architecture

### Structured retrieval

Use typed clients/SQL, never RAG, for GEO metadata, PubChem identities, registry entries, workflow state, metrics, labels, identity tables and artifact manifests. These are exact records and numerical facts.

### Keyword/full-text retrieval

Use SQLite FTS5 initially for accessions, gene symbols, endpoint/compound names, controlled vocabulary and exact phrases. Keyword retrieval must precede or accompany semantic retrieval for identifiers.

### Semantic retrieval

Use it only for publication methods, dataset descriptions, endpoint definitions, prior review rationales and scientific guidelines. Store chunks with `source`, accession, title, section, URL, retrieval timestamp, checksum, licence, document version and exact character/page span.

Chunk by document structure: titles/abstracts as units; methods/results by headings and paragraphs, generally 300–700 tokens with small overlap; tables remain structured attachments. Never mix sources in a chunk. Use a versioned embedding model behind an `EmbeddingProvider`; for the MVP, OpenAI embeddings are acceptable but the index stores model/dimension. Retrieve with FTS + vector similarity, filter by source/trust/version, fuse ranks, and optionally rerank the top 20 to 8. Citations are generated from stored exact spans, not model memory.

Stale documents are retained but marked superseded; workflows pin document checksums. Unsupported claims are rejected when no source span is attached. Prompt-injection flags reduce rank and force manual review. Do not embed raw numerical matrices, metrics tables or identities.

## Initial agents

| Agent | Inputs -> outputs | Tools | Prohibited | First slice |
|---|---|---|---|---|
| Dataset Discovery | endpoint definition/search plan -> cited candidates | search/fetch metadata, source-doc retrieval | ingest, allow-list edit, scientific acceptance | Required |
| Dataset Evaluation | candidate metadata and inspection artifacts -> comparison/recommendation | metadata, sample/control inspection, licence/provenance | compute stats in prose, approve dataset | Required; may share one harness definition with Discovery initially |
| Scientific Curation | approved source plus extracted schema -> proposed inclusion/label rules | document retrieval, structured inspection | apply unapproved labels | Required after discovery |
| Identity Resolution | conflict artifacts -> suggested resolutions | exact PubChem/RDKit evidence retrieval | silently resolve or mutate identity table | Optional in first slice; deterministic resolver is required |
| Evaluation and Audit | metrics/leakage artifacts -> cited limitations and acceptance checklist | artifact reads only | train, change thresholds, accept model | Required late in slice |
| Documentation | approved structured artifacts -> card drafts | artifact/source reads and templates | invent metrics/sources/status | P1; can be template-only for first demo |

Avoid agent-to-agent conversations. The workflow invokes the smallest relevant agent, validates its output, and stops at approvals. Expected discovery/evaluation latency is 10–45 seconds and low single-digit cents under a balanced model profile; enforce measured budgets rather than promising a fixed cost. Escalate on ambiguous endpoint definition, missing controls, conflicting methods/labels, uncertain licence/provenance, unresolved identity, insufficient data, unsupported claims or tool failure.
