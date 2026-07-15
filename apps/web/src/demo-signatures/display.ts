// Human-readable display metadata for the bundled REAL demo signatures. The demo JSON files carry
// only id / label / provenance / expected_profile / signature (schema locked by demoSignatures.test);
// this module DERIVES neutral presentation fields from that real data — it never invents values.
// The InChIKey, source and cell context are parsed from the real provenance string; the one-line
// "demonstrates" note is a plain description keyed to the demo id.

import type { DemoSignature } from "./index";

export interface DemoDisplay {
  name: string;
  context: string; // cell / assay context, from provenance
  source: string; // dataset source, from provenance
  identifier: string; // InChIKey, from provenance (falls back to id)
  demonstrates: string; // neutral, plain description of what the example shows
}

const DEMONSTRATES: Record<string, string> = {
  demo_er_high: "A strong estrogen-receptor signal with little androgen-receptor signal.",
  demo_ar_high: "A strong androgen-receptor signal with little estrogen-receptor signal.",
  demo_both_high: "Signals above threshold for both the ER and AR models.",
  demo_low_low: "A below-threshold result for both endpoints — a quiet signature.",
};

function inchikeyOf(provenance: string): string | null {
  const m = provenance.match(/InChIKey=([A-Z]{14}-[A-Z]{10}-[A-Z])/);
  return m ? m[1] : null;
}

export function demoDisplay(demo: DemoSignature): DemoDisplay {
  const p = demo.provenance;
  const isLincs = /LINCS/i.test(p);
  return {
    name: demo.label,
    context: /MCF7\+A549|MCF7 \+ A549/i.test(p)
      ? "MCF7 + A549 (condition-averaged)"
      : "Measured transcriptomic signature",
    source: isLincs ? "LINCS Level 5 (GSE92742)" : "Curated reference",
    identifier: inchikeyOf(p) ?? demo.id,
    demonstrates: DEMONSTRATES[demo.id] ?? "A real measured transcriptomic signature.",
  };
}
