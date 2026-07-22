# Security

## Current posture

EndoScan is an internal demonstrator. Its workflow controls reduce accidental or model-driven misuse, but the application does not yet implement production authentication, role-based authorization, tenant isolation, hardened network egress, managed secrets, or a server-grade deployment boundary. Do not expose it directly to untrusted users or the public internet.

## Protected assets

- API/provider credentials and environment configuration
- Human approval authority and reviewer identity
- Workflow database and immutable audit history
- Raw scientific-source artifacts and downloaded datasets
- Endpoint models, registry manifests, and unpublished benchmark results
- Local user analyses and compound inputs

## Trust boundaries

Model output, scientific-source responses, uploaded files, compound names, archive members, redirects, MIME declarations, and browser inputs are untrusted. Typed validation and deterministic code mediate them. The model-provider adapter receives only bounded context and a tool inventory; reviewed scientific adapters construct URLs from typed arguments and fixed locators.

## Implemented controls

- Strict Pydantic contracts and centralized workflow transitions
- Version-bound human approvals consumed transactionally
- Provider/tool registries and explicit capability checks
- HTTPS host allowlists, redirect checks, MIME/size/time/rate bounds
- No arbitrary model-generated URLs
- Response and archive parsing limits, path-safety checks, selective readers
- Content-addressed immutable artifacts and provenance
- Explicit budgets, attempts, retries, usage, cost, and timeout records
- Safe exception normalization and secret-like diagnostic redaction
- Cache replay without repeated transport
- Ignored local databases, caches, downloads, environments, and build output
- CI repository hygiene and secret-pattern guard

These controls are defense in depth, not proof of security.

## Secrets

Use process environment or an approved external secret manager. Never put keys in source, `.env` examples with real values, workflow artifacts, browser screenshots, logs, test fixtures, issue text, or provider arguments. Code and diagnostics must test only presence, not print values. Rotate any credential suspected of exposure.

## Source and archive safety

Provider operations use reviewed source families, operation-specific allowlists, bounded responses, and zero or explicit retries. Large archives are manifested before download. Jobs validate checksums, member names, sizes, formats, and output locations; extraction must not traverse outside the configured storage root. Cached content retains its provenance and cannot be silently substituted across an adapter/version boundary.

## Approval security

An approval must identify the exact build, gate, workflow version, and artifact/policy version. Read-only UI navigation or refresh cannot authorize work. Reusing an approval after an input mutation is prohibited. Production deployment will also need authenticated users, role separation, audit identity, session controls, CSRF protection as applicable, and independent authorization checks at every admin route.

## Reporting a vulnerability

Do not open a public issue containing exploit details, secrets, unpublished data, or personal information. Contact the repository owner through the private channel supplied to authorized collaborators. Include affected commit/version, safe reproduction steps, impact, and proposed containment.

## Production hardening backlog

Before production: identity and RBAC; managed secrets; egress proxy/network policy; TLS and secure headers; server-grade database and object storage; encryption and key management; backup/restore; audit retention; dependency/SBOM and container scanning; rate limiting; monitoring/alerting; incident response; privacy/data retention review; and independent threat modeling/penetration testing.
