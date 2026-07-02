// TIER 2 — sourced gene descriptions. Text comes ONLY from the committed, provenance-bearing
// artifact (descriptions.json, produced by scripts/extract_gene_annotations.py from NCBI Gene /
// HGNC). There is NO generated/fallback description path: a gene absent from the artifact returns
// null and renders "annotation unavailable" — never fabricated text.

import data from "./descriptions.json";
import { type GeneLink, geneLinks } from "./links";

export type { GeneLink };
export { geneLinks };

export interface GeneDescription {
  description: string;
  source: string; // e.g. "NCBI Gene"
  source_version: string; // extraction/retrieval date
  license: string; // e.g. "public domain (NLM)"
}

const GENES = (data as { genes?: Record<string, GeneDescription> }).genes ?? {};

/** The sourced description for a symbol, or null when the artifact has no real entry. */
export function geneDescription(symbol: string): GeneDescription | null {
  return GENES[symbol] ?? null;
}
