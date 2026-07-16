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

function referenceSignature(path: string) {
  const [, , , context = "ER", , encodedCompound = "CID_00"] = path.split("/");
  const compoundId = decodeURIComponent(encodedCompound);
  return {
    context,
    compound_id: compoundId,
    preferred_name: compoundId === "CID_00" ? "Caffeic Acid" : null,
    pubchem_cid: compoundId === "CID_00" ? 689043 : null,
    endpoint_label: "active",
    profile_type: "Aggregated reference profile",
    full_vector_available: true,
    underlying_condition_available: false,
    underlying_conditions: [],
    value_type: "differential_zscore",
    value_type_label: "LINCS Level 5 differential z-score",
    reference_comparison: "Calculated during LINCS processing using the corresponding experimental controls. EndoScan receives the resulting differential signature and does not choose the control.",
    source_dataset: "LINCS L1000, Level 5",
    cell_models: ["MCF7 — human breast cancer cell line", "A549 — human lung adenocarcinoma cell line"],
    aggregation_description: "This profile combines the measured experimental conditions selected for this compound when the reference map was built.",
    aggregate_pathway_warning: "This reference profile combines responses from multiple cell models. Opposing cell-specific changes may be attenuated in the aggregate.",
    feature_names: ["A1BG"],
    signature: { A1BG: 0.1 },
    n_genes: 1,
    support_row_index: 0,
    support_sha256: "fixture-support-sha256",
    source_key: "curated/ER/signatures.parquet",
    source_sha256: "fixture-source-sha256",
    provenance: { vector_origin: "support.npy exact row; never reconstructed from UMAP coordinates" },
  };
}

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
    "POST /api/interpret/biological-response": {
      body: {
        status: "ok",
        reason: null,
        input_value_type: "differential_zscore",
        increased_pathways: [
          {
            pathway_id: "R-HSA-UP",
            name: "Estrogen-dependent gene expression",
            direction: "increased",
            enrichment_statistic: 3.4,
            p_value: 0.0004,
            q_value: 0.02,
            leading_edge_genes: ["ESR1", "GREB1", "PGR"],
            pathway_size_in_universe: 14,
            statistically_supported: true,
          },
        ],
        decreased_pathways: [
          {
            pathway_id: "R-HSA-DOWN",
            name: "Cell cycle checkpoints",
            direction: "decreased",
            enrichment_statistic: -2.1,
            p_value: 0.01,
            q_value: 0.03,
            leading_edge_genes: ["CDK1", "CCNB1"],
            pathway_size_in_universe: 20,
            statistically_supported: true,
          },
        ],
        tested_gene_universe: ["ESR1", "GREB1", "PGR", "CDK1", "CCNB1"],
        method_block: {
          method: "Competitive preranked Wilcoxon rank-sum enrichment",
          method_version: "endoscan-preranked-wilcoxon-1.0.0",
          ranking_statistic: "supplied signed transcriptomic value",
          input_value_type: "differential_zscore",
          universe_size: 870,
          pathways_tested: 420,
          correction: "Benjamini-Hochberg FDR",
          min_gene_set_size: 5,
          max_gene_set_size: 500,
          leading_edge_rule: "signed tail",
          reactome: { reactome_version: "97" },
        },
      },
    },
    "POST /api/interpret/literature": {
      body: {
        endpoint_id: "ER",
        endpoint_name: "Estrogen receptor",
        status: "unavailable",
        reason: "PubMed integration is not configured.",
        articles: [],
        queries: [],
        provenance: {
          provider: "NCBI PubMed E-utilities",
          database: "pubmed",
          eutils_base_url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
          retrieved_at: "2026-07-15T00:00:00Z",
          tool: "endoscan",
          email_configured: false,
          api_key_used: false,
          rate_limit_per_second: 3,
          cache_hit: false,
        },
      },
    },
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
      const entry = routes[`${method} ${path}`]
        ?? (method === "GET" && /^\/api\/explore\/(ER|AR)\/signatures\//.test(path)
          ? { body: referenceSignature(path) }
          : undefined);
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
