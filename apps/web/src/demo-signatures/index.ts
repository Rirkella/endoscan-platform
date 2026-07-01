// Bundled demo signatures — REAL curated/staged signatures only (never fabricated/random).
// Each file is loaded at build time via Vite's import.meta.glob. Until the operator transfers
// the selected real signatures (see README.md in this directory), this list is empty and the
// Analyze page falls back to JSON paste. A demo file MUST match DemoSignature exactly.

import type { Signature } from "../api/types";

export interface DemoSignature {
  id: string;
  label: string;
  provenance: string; // real source: LINCS perturbagen/cell-line/dose/time or staged-row id
  expected_profile: { ER: number; AR: number };
  // Which endpoints can explain this on CURRENT main: both true — ER via tree_shap, AR via the
  // linear_coefficient attribution merged in PR #42. (The selecting server had a stale checkout
  // that recorded AR=false; corrected here to match main.)
  explain_available: { ER: boolean; AR: boolean };
  signature: Signature; // exactly the 978 landmark genes
}

const modules = import.meta.glob<{ default: DemoSignature }>("./*.json", { eager: true });

export const demoSignatures: DemoSignature[] = Object.values(modules)
  .map((m) => m.default)
  .sort((a, b) => a.id.localeCompare(b.id));
