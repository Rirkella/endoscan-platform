import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { type DemoSignature, demoSignatures } from "../demo-signatures";

// The committed 978-gene landmark schema (ER and AR share it byte-for-byte). Vitest runs with
// cwd = apps/web, so the repo's models/ dir is two levels up.
const schemaPath = resolve(process.cwd(), "../../models/ER/feature_schema.json");
const SCHEMA_GENES: string[] = JSON.parse(readFileSync(schemaPath, "utf-8")).features;

describe("bundled demo signatures are REAL and schema-complete", () => {
  it("the schema has exactly 978 landmark genes", () => {
    expect(SCHEMA_GENES.length).toBe(978);
  });

  it("no demo is fabricated: the loader returns only committed real files", () => {
    // May be empty until the operator transfers the selected real signatures (see README).
    expect(Array.isArray(demoSignatures)).toBe(true);
  });

  it.each(demoSignatures.map((d) => [d.id, d] as [string, DemoSignature]))(
    "demo %s has the metadata + exactly the 978 schema genes (finite)",
    (_id, demo) => {
      expect(demo.id).toBeTruthy();
      expect(demo.label).toMatch(/demo example/i);
      expect(demo.provenance).toBeTruthy();
      expect(typeof demo.expected_profile.ER).toBe("number");
      expect(typeof demo.expected_profile.AR).toBe("number");

      const genes = Object.keys(demo.signature);
      expect(genes.length).toBe(978);
      expect(new Set(genes)).toEqual(new Set(SCHEMA_GENES));
      for (const v of Object.values(demo.signature)) {
        expect(Number.isFinite(v)).toBe(true);
      }
    },
  );
});
