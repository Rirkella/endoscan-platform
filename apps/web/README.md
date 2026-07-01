# EndoScan web (`apps/web`)

A **data-driven, honesty-first** React SPA over the M7 serving API. It is a **pure API client** —
it holds no science, no model logic, and no hardcoded ER/AR metrics/statuses/limitations. Every
value shown comes from the API (`/endpoints`, `/endpoints/{id}`, `/predict`, `/explain`,
`/health`).

## Stack

React + Vite + TypeScript + Tailwind. `node_modules` is gitignored; `package.json` +
`package-lock.json` are committed.

## Run locally

1. **API** (serves the committed ER/AR models — no `dvc pull`, no credentials):

   ```bash
   uv run uvicorn endoscan_api.app:create_app --factory --port 8000
   ```

2. **Web** (Vite dev server; proxies same-origin `/api/*` → `http://127.0.0.1:8000`):

   ```bash
   cd apps/web
   npm install
   npm run dev
   ```

No credentials and no network beyond the local API are required.

## Scripts

- `npm run dev` — Vite dev server (with the `/api` proxy).
- `npm run typecheck` — `tsc --noEmit`.
- `npm test` — Vitest unit/component tests (API mocked with fixtures captured from the real API).
- `npm run build` — typecheck + production build to `dist/`.

## Configuration / deployment

- `VITE_API_BASE_URL` — the API base URL for a deployed build (no trailing slash). Unset in dev, so
  the Vite proxy is used. The SPA has no filesystem/model access; `dist/` is a static bundle
  deployable behind a reverse proxy (production CORS on the API is a deploy-time follow-up).

## Demo signatures

The Analyze page offers bundled **real** demo signatures (`src/demo-signatures/`) + JSON paste. Demo
signatures are **never fabricated/random** — see `src/demo-signatures/README.md`. Until the operator
transfers the selected real signatures, only JSON paste is available.
