# Development

## Prerequisites

- Python 3.12
- `uv` for locked Python workspace installation
- Node.js 24 and npm
- Git

Docker is optional and currently packages only the heavy-job runner. No OpenAI key, scientific-source credential, or network access is required for normal development, tests, or offline demos.

## Setup

```bash
uv run python scripts/project.py setup
```

This runs `uv sync --frozen` at the repository root and `npm ci` in `apps/web`. If the workspace is not yet installed, the equivalent bootstrap is:

```bash
uv sync --frozen
uv run python scripts/project.py setup
```

On Windows PowerShell, macOS, and Linux the Python task runner is the canonical interface. Make targets are thin optional wrappers; set `PYTHON` if the default `uv run python` is not appropriate.

## Run locally

```bash
uv run python scripts/project.py dev
```

This starts FastAPI on `127.0.0.1:8001` and Vite in its normal development mode. Run them separately when debugging:

```bash
uv run python scripts/project.py api
uv run python scripts/project.py web
```

Runtime workflow state defaults to ignored local storage. Do not point development at preserved evidence unless the task explicitly requires it.

## Command surface

List commands:

```bash
uv run python scripts/project.py --list
```

| Task | Purpose |
|---|---|
| `setup` | Locked Python and npm dependencies |
| `dev`, `api`, `web` | Development servers |
| `test`, `test-backend`, `test-workflows`, `test-api`, `test-frontend` | Test scopes |
| `lint`, `format`, `format-check` | Ruff checks/formatting |
| `typecheck` | Bounded production mypy plus full TypeScript |
| `build` | Production frontend bundle |
| `demo`, `demo-tr`, `demo-dna-damage` | Offline lifecycle fixtures |
| `docs-check`, `hygiene-check` | Links/diagrams/serialization and tracked-file safety |
| `verify` | Complete offline merge gate |

Use `--dry-run` to inspect subprocesses for a task without executing them.
Maintainer-only extraction utilities are inventoried in [`scripts/README.md`](../scripts/README.md);
they are not alternative setup or demo paths.

## Configuration

The repository ships safe offline defaults. Live model and source operation is opt-in, budgeted, and authorized by the workflow; setting an environment variable alone must not initiate a call. Never print, persist, or commit API keys. Copy no `.env` file into artifacts or screenshots.

## Database migrations

Workflow schema changes require an Alembic migration under the workflows package, upgrade coverage from the previous schema, and persistence/restart tests. Do not mutate existing migrations once published.

## Heavy-job container

Build or inspect the job-runner profile with:

```bash
docker compose --profile jobs build jobs
docker compose --profile jobs run --rm jobs
```

The root Dockerfile is not an API/web deployment stack. Local `.endoscan-data/` is mounted for job storage and remains ignored.

## Endpoint reproduction assets

`pipelines/` contains deterministic ER/AR reproduction configuration and staging helpers.
It is not the workflow engine and is not invoked by normal development commands. Real
reproduction requires separately reviewed source artifacts and explicit approval; see
[`pipelines/README.md`](../pipelines/README.md). DVC metadata is retained only for the
omitted LINCS staging matrix, and no DVC remote credential or local path is committed.

## Repository hygiene

Commit source, tests, small reviewed reference data, documentation, registry manifests, and explicitly reviewed serving artifacts only. Do not commit virtual environments, build outputs, SQLite files, provider caches, raw downloads, large matrices, demo workspaces, editor state, secrets, or machine-local paths. Run `hygiene-check` and `git diff --check` before committing.
