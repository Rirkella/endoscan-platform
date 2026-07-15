import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { type DemoSignature, demoSignatures } from "../demo-signatures";

// The committed 978-gene landmark schema (ER and AR share it byte-for-byte). Vitest runs with
// cwd = apps/web, so the repo's models/ dir is two levels up.
const schemaPath = resolve(process.cwd(), "../../models/ER/feature_schema.json");
const SCHEMA_GENES: string[] = JSON.parse(readFileSync(schemaPath, "utf-8")).features;

// Qualitative ER/AR profile each demo's recorded expected_profile must satisfy (its label).
// (Live score reproduction against the model is verified in Python at bundle time; here
// we lock the recorded profile to its label so the two can't silently drift.)
const PROFILE: Record<string, { ER: "high" | "low"; AR: "high" | "low" }> = {
  demo_low_low: { ER: "low", AR: "low" },
  demo_er_high: { ER: "high", AR: "low" },
  demo_ar_high: { ER: "low", AR: "high" },
  demo_both_high: { ER: "high", AR: "high" },
};

describe("bundled demo signatures are REAL and schema-complete", () => {
  it("the schema has exactly 978 landmark genes", () => {
    expect(SCHEMA_GENES.length).toBe(978);
  });

  it("the four real demo profiles are bundled", () => {
    const ids = new Set(demoSignatures.map((d) => d.id));
    expect(ids).toEqual(new Set(Object.keys(PROFILE)));
  });

  it.each(demoSignatures.map((d) => [d.id, d] as [string, DemoSignature]))(
    "demo %s: 978 current-main genes (finite) + metadata + corrected explain_available",
    (_id, demo) => {
      expect(demo.id).toBeTruthy();
      expect(demo.label).toBeTruthy(); // UI adds the "Demo example — real curated signature" framing
      expect(demo.provenance).toMatch(/REAL/i); // provenance states these are real, not fabricated

      // Exactly the current-main 978 landmark genes, all finite.
      const genes = Object.keys(demo.signature);
      expect(genes.length).toBe(978);
      expect(new Set(genes)).toEqual(new Set(SCHEMA_GENES));
      for (const v of Object.values(demo.signature)) {
        expect(Number.isFinite(v)).toBe(true);
      }

      // Recorded profile contains endpoint scores and matches the qualitative label.
      const { ER, AR } = demo.expected_profile;
      for (const p of [ER, AR]) expect(p).toBeGreaterThanOrEqual(0);
      for (const p of [ER, AR]) expect(p).toBeLessThanOrEqual(1);
      const q = PROFILE[demo.id];
      expect(ER >= 0.5 ? "high" : "low").toBe(q.ER);
      expect(AR >= 0.5 ? "high" : "low").toBe(q.AR);

      // Corrected for CURRENT main: both endpoints explain (ER tree_shap, AR linear_coefficient).
      expect(demo.explain_available).toEqual({ ER: true, AR: true });
    },
  );
});
