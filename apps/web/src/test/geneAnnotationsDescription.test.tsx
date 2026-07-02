// The description path reads ONLY from the artifact. Here we mock the artifact lookup to simulate
// a sourced entry and prove: (1) a gene WITH an entry shows the description + attribution;
// (2) a gene WITHOUT an entry shows "annotation unavailable" — there is no generated fallback.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../gene-annotations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../gene-annotations")>();
  return {
    ...actual, // real geneLinks (deterministic)
    geneDescription: (symbol: string) =>
      symbol === "ESR1"
        ? {
            description: "Estrogen receptor 1 (sourced summary text).",
            source: "NCBI Gene",
            source_version: "2026-06-01",
            license: "public domain (NLM)",
          }
        : null,
  };
});

// Import AFTER the mock is registered.
const { GeneAnnotation } = await import("../components/GeneAnnotation");

describe("sourced description path (Tier 2)", () => {
  it("a gene with an artifact entry shows the description + attribution", () => {
    render(<GeneAnnotation gene="ESR1" />);
    expect(screen.getByText(/Estrogen receptor 1 \(sourced summary text\)\./)).toBeInTheDocument();
    expect(screen.getByText(/source: NCBI Gene · 2026-06-01/)).toBeInTheDocument();
    // Links still present.
    expect(screen.getByRole("link", { name: "GeneCards" })).toBeInTheDocument();
  });

  it("a gene with NO entry shows only 'annotation unavailable' (no generated text)", () => {
    render(<GeneAnnotation gene="ZZZ_NOT_A_GENE" />);
    expect(screen.getByText("annotation unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/source:/i)).not.toBeInTheDocument();
  });
});
