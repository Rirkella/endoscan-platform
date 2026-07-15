import { vi } from "vitest";

import analyze from "./fixtures/analyze.json";
import catalogueSearch from "./fixtures/catalogue_search.json";
import catalogueSignature from "./fixtures/catalogue_signature.json";
import endpointAR from "./fixtures/endpoint_AR.json";
import endpointER from "./fixtures/endpoint_ER.json";
import endpoints from "./fixtures/endpoints.json";
import error404 from "./fixtures/error_404.json";
import error422 from "./fixtures/error_422_signature.json";
import explainAR from "./fixtures/explain_AR.json";
import explainER from "./fixtures/explain_ER.json";
import explore404 from "./fixtures/explore_404.json";
import exploreLocate from "./fixtures/explore_locate.json";
import exploreUmap from "./fixtures/explore_umap.json";
import health from "./fixtures/health.json";
import parseInvalid from "./fixtures/parse_invalid.json";
import parseOk from "./fixtures/parse_ok.json";
import pathwaysOk from "./fixtures/pathways_ok.json";
import pathwaysUnavailable from "./fixtures/pathways_unavailable.json";
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
    "POST /api/analyze": { body: analyze },
    "POST /api/signatures/parse": { body: parseOk },
    "GET /api/catalogue/v1/compounds": { body: catalogueSearch },
    "GET /api/catalogue/v1/signatures/lincs-gse92742-caffeic-acid-mcf7-a549": {
      body: catalogueSignature,
    },
    "GET /examples/lincs-caffeic-acid-mcf7-a549.csv": {
      body: "gene,value\nA1BG,0.1\n",
    },
    "GET /examples/lincs-cid-450-mcf7-a549.tsv": {
      body: "gene\tvalue\nA1BG\t0.1\n",
    },
    // Default to the honest "unavailable" state so existing Explain tests are undisturbed;
    // the pathways test overrides this with the ok fixture.
    "POST /api/interpret/pathways": { body: pathwaysUnavailable },
    "GET /api/explore/ER/umap": { body: exploreUmap },
    "GET /api/explore/AR/umap": { status: 404, body: explore404 },
    "POST /api/explore/locate": { body: exploreLocate },
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
      // Only JSON string bodies are parsed for resolvers; multipart FormData (uploads) is ignored.
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      const resolved = typeof entry === "function" ? entry(body) : entry;
      return new Response(JSON.stringify(resolved.body), {
        status: resolved.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

export {
  analyze,
  catalogueSearch,
  catalogueSignature,
  endpointAR,
  endpointER,
  endpoints,
  error404,
  error422,
  explainAR,
  explainER,
  explore404,
  exploreLocate,
  exploreUmap,
  parseInvalid,
  parseOk,
  pathwaysOk,
  pathwaysUnavailable,
  predictAR,
  predictER,
};
