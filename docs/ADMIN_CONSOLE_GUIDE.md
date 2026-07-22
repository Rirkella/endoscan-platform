# Admin Console Guide

## Audience and access

The Admin Console is the internal operator surface at `/admin/endpoints`. It exposes endpoint builds, state, artifacts, provider/tool traces, budgets, failures, and human review gates. The current demonstrator does not provide production-grade authentication or role-based access; run it only in a trusted local or protected environment.

## Typical review path

1. Create a build with endpoint name and biological goal.
2. Inspect the compiled specification, discovery scope, assumptions, exclusions, and unresolved questions.
3. Approve only the exact specification version you reviewed.
4. At source-discovery authorization, inspect provider capabilities, limits, and source hints before enabling any live operation.
5. Review candidate and hydrated inventories, provenance, modality, gaps, and joinability coverage.
6. At strategy review, compare proposed and rejected assembly recipes and any `ModalityAggregationPolicy`.
7. Inspect dataset preview, identifier bridge, exclusions, class distribution, leakage checks, and fingerprint before dataset approval.
8. Approve training separately; inspect benchmark metrics, applicability and limitations before scientific approval.
9. Confirm registry manifest/version and serving readiness before publication.

Never infer approval from a green status badge. The production approval record names the build, gate, artifact/version, and decision.

## Reading traces

Agent-run panels distinguish provider invocations, normal turns, retries, token usage, estimated cost, latency, tool calls, and safe diagnostics. Tool-call panels show model-supplied and normalized arguments, warnings, cache status, duration, result references, and errors. Artifact panels show producer, MIME, size, SHA-256, parents, and storage status. Raw source artifacts are evidence; normalized views are derived interpretations.

## Handling failures

Stop after a terminal provider or deterministic error unless a new run is explicitly authorized. Use the persisted error, workflow steps, immutable events, provider diagnostics, and trace artifacts—terminal access logs may omit the useful exception. Do not expose keys while troubleshooting. Zero candidates or unmet source prerequisites can be valid scientific gaps rather than execution incidents.

## Refresh and navigation

Refreshing a build page or returning with browser Back must restore the same build/state/tab from persisted reads. It must not create an agent run, OpenAI request, or scientific-source request. A silent redirect to an unrelated upload page is a defect.

## Screenshot gap

No current reviewed screenshots are committed. Stage 4 should capture, at minimum: analysis results and provenance; Projects restore/rename/delete behavior; Model Library; Explore; build overview; specification approval; source inventory and joinability; strategy review; dataset review; benchmark/scientific review; tool/provider trace; and a representative safe failure. Captures must use synthetic/offline data, hide personal information and secrets, match the final UI, and include descriptive alt text. Stale historical browser captures should not be promoted.

## Related guides

- [Endpoint Building Workflow](ENDPOINT_BUILDING_WORKFLOW.md)
- [Provider Architecture](PROVIDER_ARCHITECTURE.md)
- [Offline Demo](OFFLINE_DEMO.md)
- [Security](SECURITY.md)
