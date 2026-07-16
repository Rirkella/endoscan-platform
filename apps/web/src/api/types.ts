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
  score: number;
  /** Accepted only while older deployments migrate to `score`. */
  probability?: number;
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
export type InputValueType =
  | "differential_zscore"
  | "log2_fold_change"
  | "ranked_statistic"
  | "raw_expression";

// --- signature upload / parse (POST /signatures/parse) ---
export interface ParsePreview {
  n_detected: number;
  samples: string[] | null;
  selected_sample: string | null;
  needs_sample: boolean;
}

export interface EndpointCompatibility {
  endpoint_id: string;
  biological_target: string;
  compatible: boolean;
  n_schema_genes: number;
  n_detected: number;
  n_matched: number;
  n_missing: number;
  n_extra: number;
  missing_genes: string[];
  extra_genes: string[];
  reason: string | null;
}

export interface ParseResult {
  ready: boolean;
  format: string;
  input_value_type: InputValueType;
  preview: ParsePreview;
  compatibility: EndpointCompatibility[];
  compatible_endpoint_ids: string[];
  signature: Signature | null;
}

// --- analyze across all endpoints (POST /analyze) ---
// The API's ErrorResponse shape (no HTTP status — this is a per-endpoint entry, top-level is 200).
export interface ErrorBody {
  error: string;
  detail: string;
  endpoint_id: string | null;
  request_id: string;
}

export interface AnalyzeEndpointResult {
  endpoint_id: string;
  biological_target: string;
  model_version?: string;
  source_refs?: string[];
  ok: boolean;
  result: PredictionResult | null;
  error: ErrorBody | null;
}

export interface AnalyzeResponse {
  results: AnalyzeEndpointResult[];
  summary: {
    requested: number;
    succeeded: number;
    failed: number;
    status: "ok" | "partial" | "all_failed";
  };
}

// A structured API error (the ErrorResponse shape) surfaced to the UI. `status` is the HTTP
// code; `error`/`detail` come from the API verbatim (e.g. the gene-level 422 message).
export interface ApiError {
  status: number;
  error: string;
  detail: string;
  endpoint_id: string | null;
  request_id: string;
}

// --- verified public measured-signature catalogue (GET /catalogue/v1/*) ---
export interface CatalogueSourceBlock {
  name: string;
  accession: string | null;
  retrieved_at: string | null;
  url: string;
}

export interface CatalogueSignatureSummary {
  signature_id: string;
  compound_id: string;
  compound_name: string;
  dataset: string;
  accession: string;
  processing_level: string;
  cell_lines: string[];
  dose: string | null;
  timepoint: string | null;
  aggregation: string;
  n_genes: number;
}

export interface CatalogueCompound {
  compound_id: string;
  preferred_name: string;
  aliases: string[];
  pubchem_cid: number;
  iupac_name: string | null;
  canonical_smiles: string | null;
  isomeric_smiles: string | null;
  signatures: CatalogueSignatureSummary[];
}

export interface CatalogueSearchResponse {
  schema_version: string;
  catalogue_version: string;
  query: string;
  sources: Record<string, CatalogueSourceBlock>;
  results: CatalogueCompound[];
}

export interface CatalogueSignatureDetail extends CatalogueSignatureSummary {
  schema_version: string;
  catalogue_version: string;
  provenance: string;
  source_url: string;
  signature: Signature;
}

/** Read the current score while tolerating one release of the legacy response key. */
export function predictionScore(result: PredictionResult): number {
  if (Number.isFinite(result.score)) return result.score;
  if (Number.isFinite(result.probability)) return result.probability as number;
  throw new Error("Prediction response did not contain a numeric score.");
}

// --- Explore: the data-space (UMAP) view (GET /explore/{ctx}/umap, POST /explore/locate) ---
// UMAP is a visualization of real training data, not a boundary/proof. A new signature uses an
// approximate neighbour centroid; an exact stored record uses its committed coordinates. There
// is no in-/out-of-domain flag, only a defined isolation metric against the reference set.

export interface ExplorePoint {
  compound_id: string;
  x: number;
  y: number;
  label: string | null; // "active" | "inactive" | null (never fabricated)
  preferred_name: string | null;
  pubchem_cid: number | null;
  source_dataset: string | null;
  experimental_contexts: string[];
  full_signature_id: string | null;
}

export interface ExploreCounts {
  n_total: number;
  n_active: number;
  n_inactive: number;
  n_unlabeled: number;
}

export interface ExploreManifestSummary {
  target: string;
  n_compounds: number;
  umap: Record<string, unknown>;
  domain_metric_k: number;
  label_status: string;
  source_sha256: string;
  source_key: string;
  point_definition: string;
  aggregation: Record<string, string>;
  built_at: string | null;
}

export interface ExploreMap {
  context: string;
  points: ExplorePoint[];
  counts: ExploreCounts;
  manifest: ExploreManifestSummary;
}

export interface ExploreNeighbor {
  compound_id: string;
  distance: number;
  x: number;
  y: number;
  label: string | null;
  preferred_name: string | null;
  pubchem_cid: number | null;
  source_dataset: string | null;
  experimental_contexts: string[];
  full_signature_id: string | null;
  similarity_category: string;
  similarity_rank: number;
  similarity_percentile: number;
}

export interface ExploreDomain {
  metric: string;
  k: number;
  query_kth_distance: number;
  training_reference_quantiles: Record<string, number>;
  percentile: number;
}

export interface ExploreLocateResult {
  context: string;
  placement: "exact_existing_reference" | "approximate_nearest_neighbor";
  approx_xy: { x: number; y: number };
  neighbors: ExploreNeighbor[];
  exact_match: ExploreNeighbor | null;
  domain: ExploreDomain;
}

// --- Biological pathways (POST /interpret/pathways) ---
// Pathways over-represented among the genes that INFLUENCED THIS RESULT (model-contributing).
// Clues for investigation — never proof the compound acts through them. Technical stats live in
// the method block (rendered only in the collapsible Technical details).

export interface PathwayCard {
  pathway_id: string;
  name: string;
  description: string | null; // Reactome's own text only
  genes_influencing_result: string[]; // the actual overlap from this result
  overlap_count: number;
  pathway_size_in_universe: number;
  input_size_in_universe: number;
  p_value: number;
  q_value: number;
  evidence: string; // High | Medium | Low
}

export interface PathwayMethodBlock {
  input_gene_rule: string;
  pinned_top_n: number;
  n_toward_genes: number;
  n_input_genes: number;
  universe_size: number;
  min_pathway_overlap: number;
  test: string;
  correction: string;
  family_size: number;
  evidence_mapping: Record<string, string>;
  reactome: Record<string, unknown> | null;
}

export interface PathwaysResponse {
  endpoint_id: string;
  method: string;
  status: "ok" | "unavailable" | "too_few_genes";
  reason: string | null;
  pathways: PathwayCard[];
  exploratory_pathways: PathwayCard[];
  method_block: PathwayMethodBlock | null;
}

// --- Endpoint-independent full-signature biological response ---
export interface BiologicalPathwayCard {
  pathway_id: string;
  name: string;
  direction: "increased" | "decreased";
  enrichment_statistic: number;
  p_value: number;
  q_value: number;
  leading_edge_genes: string[];
  pathway_size_in_universe: number;
  statistically_supported: boolean;
}

export interface BiologicalResponse {
  status: "ok" | "empty" | "unavailable" | "unsupported_input";
  reason: string | null;
  input_value_type: InputValueType;
  increased_pathways: BiologicalPathwayCard[];
  decreased_pathways: BiologicalPathwayCard[];
  tested_gene_universe: string[];
  method_block: {
    method: string;
    method_version: string;
    ranking_statistic: string;
    input_value_type: InputValueType;
    universe_size: number;
    pathways_tested: number;
    correction: string;
    min_gene_set_size: number;
    max_gene_set_size: number;
    leading_edge_rule: string;
    reactome: Record<string, unknown> | null;
  } | null;
}

// --- Supporting literature (POST /interpret/literature) ---
// PubMed records matched by explicit, recorded query templates. This is contextual evidence,
// never proof that the submitted signature causes or uses a pathway.

export interface LiteraturePathwayInput {
  pathway_id: string;
  name: string;
  genes: string[];
}

export interface LiteratureQueryRecord {
  category: string;
  query: string;
  matched_genes: string[];
  matched_pathways: string[];
  pmids: string[];
}

export interface LiteratureArticle {
  pmid: string;
  title: string;
  authors: string[];
  journal: string | null;
  year: string | null;
  abstract_excerpt: string | null;
  matched_genes: string[];
  matched_pathways: string[];
  evidence_category: string;
  displayed_relationship: string;
  matched_title_terms: string[];
  matched_abstract_terms: string[];
  endpoint_concept_used: string;
  ranking_reason: string;
  relevance_reason: string;
  pubmed_url: string;
}

export interface LiteratureResponse {
  endpoint_id: string;
  endpoint_name: string;
  status: "ok" | "empty" | "unavailable" | "rate_limited" | "timeout";
  reason: string | null;
  articles: LiteratureArticle[];
  queries: LiteratureQueryRecord[];
  provenance: {
    provider: string;
    database: string;
    eutils_base_url: string;
    retrieved_at: string;
    tool: string;
    email_configured: boolean;
    api_key_used: boolean;
    rate_limit_per_second: number;
    cache_hit: boolean;
  };
}
