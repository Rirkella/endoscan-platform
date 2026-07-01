// TypeScript mirror of the M7 serving-API contract (services/api). The API is the source of
// truth; these types only describe shapes so the SPA stays a thin, data-driven client.

export interface Health {
  status: string;
  endpoints_loaded: string[];
  explain_available: boolean;
}

export type EndpointStatus = string; // e.g. "experimental" — rendered as returned, never assumed

export interface EndpointSummary {
  endpoint_id: string;
  biological_target: string;
  status: EndpointStatus;
  input_type: string;
  frozen: boolean;
}

export interface LimitationsBlock {
  endpoint_id: string;
  status: EndpointStatus;
  is_experimental: boolean;
  missed_criteria: string[];
  floors_recorded: boolean;
  floors_provenance: string | null;
  positives: number | null;
  n_total: number | null;
  prevalence: number | null;
  claim_scope: string;
  disclaimer: string;
}

export interface MetricCI {
  point: number;
  lo: number;
  hi: number;
  n_resamples?: number;
}

export interface MetricsSummary {
  auroc: number | null;
  auprc: number | null;
  balanced_accuracy: number | null;
  brier_score: number | null;
  // Full uncertainty block from metrics.json (per-metric {point,lo,hi} + method/ci_level/note).
  uncertainty: Record<string, unknown> | null;
  evidence: Record<string, unknown> | null;
}

export interface ContextBlock {
  cell_lines: string[] | null;
  label_sources: string[];
  scope: string | null;
}

export interface ContextVariant {
  variant_id: string;
  context: ContextBlock;
  status: EndpointStatus;
  limitations: LimitationsBlock;
  metrics_summary: MetricsSummary;
  model_card_markdown: string;
}

export interface EndpointDetail {
  endpoint_id: string;
  biological_target: string;
  input_type: string;
  version: string;
  status: EndpointStatus;
  frozen: boolean;
  source_refs: string[];
  variants: ContextVariant[];
}

export interface PredictionResult {
  endpoint_id: string;
  probability: number;
  call: boolean;
  threshold: number;
  standardized_input: boolean;
  limitations: LimitationsBlock;
}

export type AttributionMethodName = "tree_shap" | "linear_coefficient" | string;

export interface GeneAttribution {
  gene: string;
  // Named shap_value in the API for historical reasons; its MEANING depends on `method`
  // (see ExplanationResult.method) — a SHAP value for tree_shap, a coefficient×value
  // contribution for linear_coefficient.
  shap_value: number;
  direction: "toward" | "away" | string;
}

export interface ExplanationResult {
  endpoint_id: string;
  method: AttributionMethodName;
  base_value: number;
  n_features: number;
  top_contributors: GeneAttribution[];
  limitations: LimitationsBlock;
}

export type Signature = Record<string, number>;

// --- signature upload / parse (POST /signatures/parse) ---
export interface ParsePreview {
  n_detected: number;
  n_matched: number;
  n_missing: number;
  n_extra: number;
  missing_genes: string[];
  extra_genes: string[];
  samples: string[] | null;
  selected_sample: string | null;
  needs_sample: boolean;
}

export interface ParseResult {
  aligned: boolean;
  format: string;
  schema_endpoint_id: string;
  n_schema_genes: number;
  preview: ParsePreview;
  signature: Signature | null;
}

// --- analyze across all endpoints (POST /analyze) ---
// The API's ErrorResponse shape (no HTTP status — this is a per-endpoint entry, top-level is 200).
export interface ErrorBody {
  error: string;
  detail: string;
  endpoint_id: string | null;
}

export interface AnalyzeEndpointResult {
  endpoint_id: string;
  biological_target: string;
  ok: boolean;
  result: PredictionResult | null;
  error: ErrorBody | null;
}

export interface AnalyzeResponse {
  results: AnalyzeEndpointResult[];
}

// A structured API error (the ErrorResponse shape) surfaced to the UI. `status` is the HTTP
// code; `error`/`detail` come from the API verbatim (e.g. the gene-level 422 message).
export interface ApiError {
  status: number;
  error: string;
  detail: string;
  endpoint_id: string | null;
}
