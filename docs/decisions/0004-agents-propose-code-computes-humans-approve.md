# ADR 0004: Agents propose, deterministic code computes, humans approve

## Context

Language models are useful for bounded synthesis but cannot be the authority for source access, joins, labels, compute, or publication.

## Decision

Agents return typed proposals and reviewed tool requests. Deterministic code validates, retrieves, parses, joins, assembles, trains, and fingerprints. Humans approve versioned scientific decisions and costly/downstream transitions.

## Consequences

Every boundary is testable and auditable; invalid model output stops safely. More contracts and review steps are required, and automation cannot bypass a gate for convenience.

## Status

Accepted.
