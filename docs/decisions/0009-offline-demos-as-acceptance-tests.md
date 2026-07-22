# ADR 0009: Offline demonstrations as architecture acceptance tests

## Context

Full live workflows are costly, mutable, credentialed, and unsuitable for deterministic CI.

## Decision

Use prepared TR and DNA-damage fixtures to execute the complete governed lifecycle offline as architecture acceptance tests. Keep live smokes separately authorized and separately reported.

## Consequences

CI can prove lifecycle wiring, replay, gates, artifacts, and endpoint generality. Fixtures cannot be represented as discoveries or scientific validation and must be supplemented by bounded live and expert review.

## Status

Accepted.
