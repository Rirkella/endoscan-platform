// Thin typed fetch client for the EndoScan serving API. Base URL comes from
// VITE_API_BASE_URL (deploys); in dev it is empty and the Vite proxy forwards /api -> :8000.
// Every non-2xx response is normalized into a thrown ApiError carrying the API's own
// {error, detail} (e.g. the gene-level 422 message) so the UI can render it verbatim.

import type {
  AnalyzeResponse,
  ApiError,
  EndpointDetail,
  EndpointSummary,
  ExplanationResult,
  ExploreLocateResult,
  ExploreMap,
  Health,
  ParseResult,
  PredictionResult,
  Signature,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function url(path: string): string {
  // With a configured base, call it directly; otherwise use the same-origin /api proxy prefix.
  return BASE ? `${BASE}${path}` : `/api${path}`;
}

export class EndoscanApiError extends Error implements ApiError {
  status: number;
  error: string;
  detail: string;
  endpoint_id: string | null;

  constructor(e: ApiError) {
    super(e.detail || e.error);
    this.name = "EndoscanApiError";
    this.status = e.status;
    this.error = e.error;
    this.detail = e.detail;
    this.endpoint_id = e.endpoint_id;
  }
}

async function toError(res: Response): Promise<EndoscanApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // non-JSON error body
  }
  const b = (body ?? {}) as Record<string, unknown>;
  // FastAPI pydantic 422 uses {detail: [...]}; our handlers use {error, detail, endpoint_id}.
  const detail =
    typeof b.detail === "string"
      ? b.detail
      : b.detail
        ? JSON.stringify(b.detail)
        : res.statusText;
  return new EndoscanApiError({
    status: res.status,
    error: typeof b.error === "string" ? b.error : `http_${res.status}`,
    detail,
    endpoint_id: typeof b.endpoint_id === "string" ? b.endpoint_id : null,
  });
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(url(path), { headers: { accept: "application/json" } });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, payload: unknown): Promise<T> {
  const res = await fetch(url(path), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

export const api = {
  health: () => getJson<Health>("/health"),
  listEndpoints: () => getJson<EndpointSummary[]>("/endpoints"),
  getEndpoint: (id: string) => getJson<EndpointDetail>(`/endpoints/${encodeURIComponent(id)}`),
  predict: (endpoint_id: string, signature: Signature, allow_extra = false) =>
    postJson<PredictionResult>("/predict", { endpoint_id, signature, allow_extra }),
  explain: (endpoint_id: string, signature: Signature, top_n = 10, allow_extra = false) =>
    postJson<ExplanationResult>("/explain", { endpoint_id, signature, top_n, allow_extra }),

  // Run a signature across ALL endpoints in one call (per-endpoint isolation server-side).
  analyze: (signature: Signature, allow_extra = false) =>
    postJson<AnalyzeResponse>("/analyze", { signature, allow_extra }),

  // Explore: the committed data-space (UMAP) map for a context (404 when not yet computed).
  exploreUmap: (context: string) =>
    getJson<ExploreMap>(`/explore/${encodeURIComponent(context)}/umap`),
  // Place a signature by nearest neighbours (approximate; never an exact projection).
  exploreLocate: (context: string, signature: Signature, allow_extra = false) =>
    postJson<ExploreLocateResult>("/explore/locate", { context, signature, allow_extra }),

  // Upload parse + validate (multipart). The server is the ONE gene validator — the client
  // never validates genes; a 4xx here carries the API's verbatim message.
  parseSignature: async (opts: {
    file?: File;
    content?: string;
    format: "json" | "csv";
    allow_extra?: boolean;
    sample?: string | null;
  }): Promise<ParseResult> => {
    const fd = new FormData();
    if (opts.file) fd.append("file", opts.file);
    if (opts.content != null) fd.append("content", opts.content);
    fd.append("format", opts.format);
    if (opts.allow_extra) fd.append("allow_extra", "true");
    if (opts.sample) fd.append("sample", opts.sample);
    const res = await fetch(url("/signatures/parse"), { method: "POST", body: fd });
    if (!res.ok) throw await toError(res);
    return (await res.json()) as ParseResult;
  },
};
