import { vi } from "vitest";

import endpointAR from "./fixtures/endpoint_AR.json";
import endpointER from "./fixtures/endpoint_ER.json";
import endpoints from "./fixtures/endpoints.json";
import error404 from "./fixtures/error_404.json";
import error422 from "./fixtures/error_422_signature.json";
import explainAR from "./fixtures/explain_AR.json";
import explainER from "./fixtures/explain_ER.json";
import health from "./fixtures/health.json";
import predictAR from "./fixtures/predict_AR.json";
import predictER from "./fixtures/predict_ER.json";

type Resolver = { status?: number; body: unknown } | ((body: unknown) => { status?: number; body: unknown });

// Default routes wired to the captured real-API fixtures. Tests override any key.
function defaultRoutes(): Record<string, Resolver> {
  return {
    "GET /api/health": { body: health },
    "GET /api/endpoints": { body: endpoints },
    "GET /api/endpoints/ER": { body: endpointER },
    "GET /api/endpoints/AR": { body: endpointAR },
    "GET /api/endpoints/NOPE": { status: 404, body: error404 },
    "POST /api/predict": (b) => ({
      body: (b as { endpoint_id: string }).endpoint_id === "ER" ? predictER : predictAR,
    }),
    "POST /api/explain": (b) => ({
      body: (b as { endpoint_id: string }).endpoint_id === "ER" ? explainER : explainAR,
    }),
  };
}

export function installFetchMock(overrides: Record<string, Resolver> = {}): void {
  const routes = { ...defaultRoutes(), ...overrides };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input.toString();
      const path = new URL(raw, "http://localhost").pathname;
      const method = (init?.method ?? "GET").toUpperCase();
      const entry = routes[`${method} ${path}`];
      if (!entry) return new Response(JSON.stringify({ detail: "unmocked" }), { status: 500 });
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      const resolved = typeof entry === "function" ? entry(body) : entry;
      return new Response(JSON.stringify(resolved.body), {
        status: resolved.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

export {
  endpointAR,
  endpointER,
  endpoints,
  error404,
  error422,
  explainAR,
  explainER,
  predictAR,
  predictER,
};
