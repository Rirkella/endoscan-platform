# ADR 0007: Compound-grouped splitting

## Context

Multiple assays or expression profiles for the same compound can leak identity across train and evaluation sets, inflating performance.

## Decision

Benchmark splits group by canonical compound identity unless a stricter reviewed grouping is required. Replicates and contexts for one compound do not cross the relevant split boundary.

## Consequences

Metrics better reflect generalization to unseen compounds and may be lower or more uncertain. Unresolved identities cannot be casually split and require exclusion or explicit policy.

## Status

Accepted.
