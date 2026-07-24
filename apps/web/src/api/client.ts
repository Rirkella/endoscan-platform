// Thin typed fetch client for the EndoScan serving API. Base URL comes from
// VITE_API_BASE_URL (deploys); in dev it is empty and the Vite proxy forwards /api -> :8000.
// Every non-2xx response is normalized into a thrown ApiError carrying the API's own
// {error, detail} (e.g. the gene-level 422 message) so the UI can render it verbatim.

import type {
  AnalyzeResponse,
  AdminAgentRun,
  AdminAgentCapabilities,
  AdminApproval,
  AdminArtifact,
  AdminBuild,
  AdminProviderPreflight,
  AdminTimelineEvent,
  AdminTrainingDatasetWorkflow,
  AdminWorkflowError,
  ApiError,
  BiologicalResponse,
  CatalogueSearchResponse,
  CatalogueSignatureDetail,
  EndpointDetail,
  EndpointSummary,
  ExplanationResult,
  ExploreLocateResult,
  ExploreMap,
  ExploreReferenceSignature,
  Health,
  InputValueType,
  LiteraturePathwayInput,
  LiteratureResponse,
  ParseResult,
  PathwaysResponse,
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
  request_id: string;

  constructor(e: ApiError) {
    super(e.detail || e.error);
    this.name = "EndoscanApiError";
    this.status = e.status;
    this.error = e.error;
    this.detail = e.detail;
    this.endpoint_id = e.endpoint_id;
    this.request_id = e.request_id;
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
    request_id: typeof b.request_id === "string" ? b.request_id : "unavailable",
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

const ADMIN_HEADERS = { "X-EndoScan-Admin": "local-development" };

function commandKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

async function adminGet<T>(path: string): Promise<T> {
  const res = await fetch(url(path), {
    headers: { accept: "application/json", ...ADMIN_HEADERS },
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

async function adminPost<T>(path: string, payload: unknown, prefix: string): Promise<T> {
  const res = await fetch(url(path), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      "Idempotency-Key": commandKey(prefix),
      ...ADMIN_HEADERS,
    },
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

  // Run only explicitly selected compatible endpoints (or all when omitted).
  analyze: (
    signature: Signature,
    endpoint_ids?: string[],
    allow_extra = false,
    input_value_type: InputValueType = "ranked_statistic",
  ) => postJson<AnalyzeResponse>("/analyze", {
    signature,
    endpoint_ids,
    allow_extra,
    input_value_type,
  }),

  searchCatalogue: (query: string, limit = 10) =>
    getJson<CatalogueSearchResponse>(
      `/catalogue/v1/compounds?query=${encodeURIComponent(query)}&limit=${limit}`,
    ),
  getCatalogueSignature: (signatureId: string) =>
    getJson<CatalogueSignatureDetail>(
      `/catalogue/v1/signatures/${encodeURIComponent(signatureId)}`,
    ),

  // Biological pathways for an explain result (Reactome over-representation; honest empty states).
  interpretPathways: (endpoint_id: string, signature: Signature, allow_extra = false) =>
    postJson<PathwaysResponse>("/interpret/pathways", { endpoint_id, signature, allow_extra }),

  interpretBiologicalResponse: (signature: Signature, input_value_type: InputValueType) =>
    postJson<BiologicalResponse>("/interpret/biological-response", {
      signature,
      input_value_type,
    }),

  interpretLiterature: (
    endpoint_id: string,
    genes: string[],
    pathways: LiteraturePathwayInput[],
    compound?: string,
    context?: string,
    response_genes: string[] = [],
  ) =>
    postJson<LiteratureResponse>("/interpret/literature", {
      endpoint_id,
      genes,
      pathways,
      compound,
      context,
      response_genes,
    }),

  // Explore: the committed data-space (UMAP) map for a context (404 when not yet computed).
  exploreUmap: (context: string) =>
    getJson<ExploreMap>(`/explore/${encodeURIComponent(context)}/umap`),
  getReferenceSignature: (context: string, compoundId: string) =>
    getJson<ExploreReferenceSignature>(
      `/explore/${encodeURIComponent(context)}/signatures/${encodeURIComponent(compoundId)}`,
    ),
  // Place by nearest neighbours; exact stored records use their committed map coordinates.
  exploreLocate: (context: string, signature: Signature, allow_extra = false) =>
    postJson<ExploreLocateResult>("/explore/locate", { context, signature, allow_extra }),

  // Upload parse + validate (multipart). The server is the ONE gene validator — the client
  // never validates genes; a 4xx here carries the API's verbatim message.
  parseSignature: async (opts: {
    file?: File;
    content?: string;
    format: "json" | "csv" | "tsv";
    allow_extra?: boolean;
    sample?: string | null;
    input_value_type?: InputValueType;
  }): Promise<ParseResult> => {
    const fd = new FormData();
    if (opts.file) fd.append("file", opts.file);
    if (opts.content != null) fd.append("content", opts.content);
    fd.append("format", opts.format);
    if (opts.allow_extra) fd.append("allow_extra", "true");
    if (opts.sample) fd.append("sample", opts.sample);
    if (opts.input_value_type) fd.append("input_value_type", opts.input_value_type);
    const res = await fetch(url("/signatures/parse"), { method: "POST", body: fd });
    if (!res.ok) throw await toError(res);
    return (await res.json()) as ParseResult;
  },

  adminListBuilds: () => adminGet<AdminBuild[]>("/admin/endpoint-builds"),
  adminCapabilities: () => adminGet<AdminAgentCapabilities>("/admin/capabilities"),
  adminProviderPreflight: () =>
    adminPost<AdminProviderPreflight>("/admin/agent-provider/preflight", {}, "provider-preflight"),
  adminGetBuild: (id: string) =>
    adminGet<AdminBuild>(`/admin/endpoint-builds/${encodeURIComponent(id)}`),
  adminTrainingDatasetWorkflow: (id: string) =>
    adminGet<AdminTrainingDatasetWorkflow>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/training-dataset-workflow`,
    ),
  adminCreateBuild: (payload: {
    endpoint_name: string;
    endpoint_slug: string;
    biological_goal: string;
    created_by: string;
    workflow_kind?: "legacy_single_source_discovery" | "training_dataset_discovery";
    benchmark_mode?: string;
  }) => adminPost<AdminBuild>("/admin/endpoint-builds", payload, "create-build"),
  adminCommand: (
    id: string,
    action: "start" | "pause" | "resume" | "cancel" | "retry" | "simulate-failure" | "retry-dataset-specification" | "run-dataset-specification-review" | "revise-endpoint-request",
    version: number,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/${action}`,
      { expected_version: version, actor: "local-admin" },
      action,
    ),
  adminRefreshSourceMetadata: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/refresh-source-metadata`,
      { expected_version: version, actor: "local-admin" },
      "refresh-source-metadata",
    ),
  adminContinueTrainingDataset: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/continue-training-dataset`,
      { expected_version: version, actor: "local-admin" },
      "continue-training-dataset",
    ),
  adminAuthorizeSourceDiscovery: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/authorize-source-discovery`,
      {
        expected_version: version,
        actor: "local-admin",
        confirmation: "authorize_reviewed_source_discovery",
      },
      "authorize-source-discovery",
    ),
  adminRequestDiscoveryRevision: (
    id: string,
    version: number,
    reason: string,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/request-discovery-revision`,
      {
        expected_version: version,
        actor: "local-admin",
        reason,
        provider_policy_revision: {},
        modality_policy_revision: {},
        context_constraint_revision: {},
      },
      "request-discovery-revision",
    ),
  adminCalculateCombinationCoverage: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/calculate-combination-coverage`,
      { expected_version: version, actor: "local-admin" },
      "calculate-combination-coverage",
    ),
  adminGenerateAssemblyStrategies: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/generate-assembly-strategies`,
      { expected_version: version, actor: "local-admin" },
      "generate-assembly-strategies",
    ),
  adminApproveAssemblyStrategy: (
    id: string,
    version: number,
    proposal: Record<string, unknown>,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/approve-assembly-strategy`,
      {
        expected_version: version,
        actor: "local-admin",
        strategy_proposal_id: proposal.proposal_id,
        approved_modality_aggregation: {
          source_modalities: proposal.modalities ?? [],
          operator:
            (proposal.proposed_label_policy as Record<string, unknown> | undefined)?.operator ===
            "functional_or"
              ? "functional_or"
              : "none",
          preserve_original_source_records: true,
          human_approved: true,
        },
        approved_context_filters: proposal.proposed_context ?? {},
        approved_dose_time_rules: {
          require_source_backed_values: true,
          preserve_missingness: true,
        },
        approved_label_policy: proposal.proposed_label_policy ?? {},
        exclusion_rules: proposal.exclusions ?? [],
        required_extraction_fields: [
          "canonical_compound_identifier",
          "source_activity_record",
          "transcriptomic_signature_identifier",
          "experimental_context",
          "provenance",
        ],
      },
      `approve-assembly-strategy-${String(proposal.proposal_id)}`,
    ),
  adminRejectAssemblyStrategies: (
    id: string,
    version: number,
    reason: string,
    options: {
      reason_category?: string;
      revision_objective?: string;
      technical_proof_classification?: "TECHNICAL_PROOF_OF_JOINABILITY";
      technical_proof_proposal_ids?: string[];
    } = {},
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/reject-assembly-strategies`,
      { expected_version: version, actor: "local-admin", reason, ...options },
      "reject-assembly-strategies",
    ),
  adminRunApprovedAssembly: (
    id: string,
    version: number,
    offlineFixtureId: "tr_receptor" | "dna_damage",
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/run-approved-assembly`,
      {
        expected_version: version,
        actor: "local-admin",
        offline_fixture_id: offlineFixtureId,
      },
      "run-approved-assembly",
    ),
  adminApproveDataset: (id: string, version: number, rationale: string) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/approve-dataset`,
      { expected_version: version, actor: "local-admin", rationale },
      "approve-dataset",
    ),
  adminReviewDatasetRevision: (
    id: string,
    version: number,
    decision: "rejected" | "revision_requested",
    rationale: string,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/review-dataset-revision`,
      { expected_version: version, actor: "local-admin", decision, rationale },
      `review-dataset-${decision}`,
    ),
  adminRunBenchmark: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/run-benchmark`,
      { expected_version: version, actor: "local-admin" },
      "run-benchmark",
    ),
  adminSelectAndValidateModel: (
    id: string,
    version: number,
    candidateId: string,
    rationale: string,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/select-and-validate-model`,
      {
        expected_version: version,
        actor: "local-admin",
        candidate_id: candidateId,
        decision_threshold: 0.5,
        rationale,
      },
      `select-and-validate-${candidateId}`,
    ),
  adminReviewModels: (
    id: string,
    version: number,
    decision: "reject_all_models" | "request_new_benchmark",
    rationale: string,
  ) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/review-models`,
      { expected_version: version, actor: "local-admin", decision, rationale },
      `review-models-${decision}`,
    ),
  adminPublishEndpoint: (id: string, version: number) =>
    adminPost<AdminBuild>(
      `/admin/endpoint-builds/${encodeURIComponent(id)}/publish-endpoint`,
      { expected_version: version, actor: "local-admin" },
      "publish-endpoint",
    ),
  adminTimeline: (id: string) =>
    adminGet<AdminTimelineEvent[]>(`/admin/endpoint-builds/${encodeURIComponent(id)}/timeline`),
  adminArtifacts: (id: string) =>
    adminGet<AdminArtifact[]>(`/admin/endpoint-builds/${encodeURIComponent(id)}/artifacts`),
  adminArtifactPreview: (id: string) =>
    adminGet<{ artifact: AdminArtifact; content: unknown }>(
      `/admin/artifacts/${encodeURIComponent(id)}/preview`,
    ),
  adminApprovals: (id: string) =>
    adminGet<AdminApproval[]>(`/admin/endpoint-builds/${encodeURIComponent(id)}/approvals`),
  adminDecideApproval: (
    approval: AdminApproval,
    buildVersion: number,
    decision: "approve" | "reject" | "request_revision" | "choose_alternative",
    reviewerComment: string,
    selectedAlternativeId?: string,
  ) =>
    adminPost<AdminBuild>(
      `/admin/approvals/${encodeURIComponent(approval.id)}/decisions`,
      {
        decision,
        reviewer_id: "local-admin",
        reviewer_comment: reviewerComment,
        selected_alternative_id: selectedAlternativeId ?? null,
        expected_version: buildVersion,
        artifact_hashes: approval.request.artifact_hashes,
      },
      `approval-${decision}`,
    ),
  adminAgentRuns: (id: string) =>
    adminGet<AdminAgentRun[]>(`/admin/endpoint-builds/${encodeURIComponent(id)}/agent-runs`),
  adminAgentRun: (id: string) =>
    adminGet<AdminAgentRun>(`/admin/agent-runs/${encodeURIComponent(id)}`),
  adminErrors: (id: string) =>
    adminGet<AdminWorkflowError[]>(`/admin/endpoint-builds/${encodeURIComponent(id)}/errors`),
};
