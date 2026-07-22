# ADR 0008: Explicit validation-status model

## Context

Implemented code, passing fixtures, successful live transport, and scientific validation are different kinds of evidence.

## Decision

Track and report implementation, offline validation, live technical validation, and scientific validation separately. Never infer a later status from an earlier one.

## Consequences

Product claims remain honest and gaps are easy to prioritize. Status reporting takes maintenance and may expose incompleteness that a single “done” label would hide.

## Status

Accepted.
