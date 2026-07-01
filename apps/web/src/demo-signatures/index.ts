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
  signature: Signature; // exactly the 978 landmark genes
}

const modules = import.meta.glob<{ default: DemoSignature }>("./*.json", { eager: true });

export const demoSignatures: DemoSignature[] = Object.values(modules)
  .map((m) => m.default)
  .sort((a, b) => a.id.localeCompare(b.id));
