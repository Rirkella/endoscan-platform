# ADR 0003: Two-layer product design

## Context

Serving a reviewed endpoint is a different trust boundary from discovering evidence and building a new one.

## Decision

Separate the user analysis layer from the governed endpoint-building layer while sharing registry, contracts, artifacts, and APIs. Analysis reads registered artifacts; building proceeds through the Admin Console and approvals.

## Consequences

Normal analysis cannot silently launch discovery or training. Builder complexity is isolated, while registry compatibility becomes a critical shared contract.

## Status

Accepted.
