# Testing

## Merge gates

The canonical offline gate is:

```bash
uv run python scripts/project.py verify
```

It runs Ruff lint and format checks, bounded production mypy, TypeScript, all Python and frontend tests, the production frontend build, documentation/serialization/hygiene audits, and both offline endpoint demos. CI separates Python and frontend jobs, applies the same checks, and adds a high-severity npm dependency audit after locked installation.

## Test layers

| Layer | What it covers | Command |
|---|---|---|
| Core/unit | Registry, inference, features, splits, training, diagnostics | `uv run python scripts/project.py test-backend` |
| Workflow | State machine, migrations, approvals, providers, artifacts, replay, lifecycle | `uv run python scripts/project.py test-workflows` |
| API | Analysis and admin routes, validation, persistence contracts | `uv run python scripts/project.py test-api` |
| Frontend | Components, routes, saved-state behavior, admin workflows | `uv run python scripts/project.py test-frontend` |
| End-to-end offline | Complete TR and DNA-damage governed lifecycles | `uv run python scripts/project.py demo` |
| Static/build | Ruff, mypy, TypeScript, Vite production build | `lint`, `format-check`, `typecheck`, `build` |
| Repository | Markdown links/diagrams, JSON/TOML, tracked secrets/paths/runtime debris | `docs-check`, `hygiene-check` |

## Provider tests

Provider tests use immutable fixtures and transport test doubles. Required cases include successful parsing; empty and partial results; missing identifiers; typed batching; pagination/completion; redirects and allowlists; MIME and response-size enforcement; timeout; zero retries where configured; raw artifact persistence; provenance; cache hit with zero second transport; sanitized errors; and unsupported or unavailable provider capability.

Live smokes are not CI tests. They require explicit authorization, fixed reviewed operations, minimal bounded transport, safe diagnostics, and a separate report. A live smoke must never become a hidden prerequisite for an offline suite.

## Workflow acceptance

Tests should prove state and version, approval binding, event/artifact lineage, restart behavior, budget enforcement, no duplicate provider generation, preservation of partial results, and no implicit source calls on reads or browser refresh. Zero candidates and dependency gaps are expected outcomes when represented by valid contracts.

## Scientific tests

Scientific fixtures test invariants: modalities remain distinguishable; PubChem AIDs are not compound IDs; identity joins require evidence; binding is not silently functional activity; aggregation requires a versioned approved policy; splits avoid leakage; outputs declare limitations and applicability. Fixture truth is test truth only.

## Browser acceptance

Before a release, exercise saved-analysis refresh, project rename/delete isolation, navigate-away/back restoration, InChIKey-only reference-profile restoration, and every Admin Console gate. Observe console and network logs for errors, HTTP 500s, silent redirects, and automatic OpenAI/scientific-source calls. The final repository audit verified routes, startup, API boundaries and the offline UI suite; current interactive browser evidence remains a separate release artifact.

## Adding tests

Place package-local unit tests with the package and cross-cutting regressions under `tests/`. Tests must be deterministic, offline by default, and safe in parallel. Use temporary directories and databases; never depend on a developer's `.endoscan/` state.
