# Agent-system security threat model

## Assets and trust boundaries

Critical assets are model/provider keys, approved source data, human decisions, immutable artifacts, endpoint registry, trained model files and audit traces. Trust boundaries separate the browser, admin API, workflow DB, agent/provider, tool executor, external scientific sources, artifact store, heavy worker and publisher.

External publications, metadata, archives, URLs and model output are untrusted. Deterministic tool results become trusted only after schema validation, checksum/provenance recording and applicable human approval. Existing `model.pkl` loading is trusted-repository-only; an uploaded or agent-produced pickle must never be loaded before promotion validation.

## Threats and controls

| Threat | Impact | Required controls |
|---|---|---|
| Prompt injection in papers/descriptions | policy/tool override, false recommendation | delimit untrusted text; fixed system policy; no model browsing/shell; injection classifier/flags; citation and human review |
| Malicious URLs/SSRF | internal network or credential access | per-tool HTTPS domain allowlists; resolve and reject private/link-local/loopback IPs; validate redirects; no caller-controlled headers/proxy |
| Arbitrary shell/filesystem | host takeover or tampering | no shell tool; typed functions only; generated artifact IDs; read/write roots per tool; sandbox worker user/container |
| Path traversal | overwrite registry/secrets | reuse `storage.normalize_key`; resolve-under-root checks; reject absolute/`..`; never accept output paths from the model |
| Secret/API-key leakage | account compromise | process secret store/env injection only; redaction; keys absent from prompts, tool results, DB context and serialized RunState; scoped keys and rotation |
| Unsafe downloads | decompression bomb/malware/data execution | content-length and decompressed-size caps; media/magic validation; checksum; quarantine; archive member/path/count limits; never execute content |
| Unverified data execution/pickle | code execution | parse data with safe libraries; no macros/scripts; train in sandbox; publish only approved artifacts; migrate serving to a safer vetted serialization when feasible |
| Registration without approval | unreviewed endpoint in product | separate model-acceptance and registration approvals bound to hashes; publisher service account only; agent lacks registry permission |
| Artifact tampering | wrong data/model after review | SHA-256, immutable naming, append-only events, approval binds artifact-set hash, verify immediately before publish |
| Compromised tool/MCP | false evidence or mutation | no external MCP for P0; local signed/versioned tool registry; least privilege; output schemas; independent source/checksum validation |
| Identity/label ambiguity | scientifically invalid model | deterministic conflicts; mandatory approval; no default resolution or majority vote |
| Denial of service | unavailable demo/cost | request/body/download caps; queues; concurrency 1 per build; rate limits; timeouts; cancellation; cache and circuit breaker |
| Unbounded token/tool cost | budget exhaustion | per-run turns/tool/tokens/cost; global daily ceiling; usage persistence; fail closed at budget |
| Cross-build/user access | data/approval disclosure | authenticated admin role, object-level authorization, workflow ownership, audit reads, CSRF protection if cookie auth |
| Replay/race/double publish | duplicate work/registry corruption | idempotency keys, optimistic locking, unique constraints, atomic publisher, publish reference recorded |
| Trace leakage | source/secrets/PII exposure | sensitive capture disabled by default, field redaction, access control, retention policy, encrypted storage |
| Supply-chain compromise | malicious dependencies/actions | lockfiles, dependency scanning, pinned Actions, minimal images, SBOM/signatures for release artifacts |

## Per-tool permission model

Each tool declares: semantic version, input/output schemas, network domains, readable artifact kinds, writable artifact kinds, maximum bytes/runtime, idempotency class, required workflow states and required approval before/after.

- Discovery: network read only to approved metadata APIs; writes source-document/candidate artifacts.
- Download: only an already approved candidate URL/locator; quarantined raw namespace; size/checksum required.
- Inspection/identity/audit: reads pinned artifacts; writes new immutable reports; no network except explicit PubChem identity tool.
- Training: reads an approved candidate-table/config hash; writes candidate model artifacts; isolated worker; no outbound network.
- Registration: not exposed to agents. Publisher reads an approved endpoint-candidate hash and writes only the registry/artifact release destination.

## Approval and audit controls

Endpoint definition, dataset selection, label rules, unresolved identity, training authorization, model acceptance and registration are mandatory gates. UI actions record authenticated reviewer, role, comment, timestamp, proposal and artifact hashes. Events are append-only and hash-linked. A stale approval cannot authorize a revised artifact. Rejection and revision cannot be overwritten by an agent retry.

## Demo deployment baseline

- Bind services to localhost or a protected internal environment; do not expose the admin console anonymously.
- SQLite DB and artifacts live on a persistent encrypted disk with backup before demo.
- Worker runs as a non-admin container/user with a per-run scratch directory and network disabled for training.
- Provider and external-source egress use explicit allowlists. Set low concurrency/rate/cost ceilings.
- Preflight verifies DB migrations, source-cache checksums, prepared artifact hashes, model key availability, publisher credentials and rollback reference.
- If any integrity or approval verification fails, demonstrate the safe refusal rather than bypassing it.
