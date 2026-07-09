// Gene annotations — TIER 1 deterministic links (always) + TIER 2 sourced descriptions from the
// committed NCBI Gene artifact (305/978 landmarks covered); genes with no entry render "annotation
// unavailable". No fabricated text anywhere.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExplanationResult } from "../api/types";
import { GeneAnnotation } from "../components/GeneAnnotation";
import { GeneContributionCards } from "../components/GeneContributionCards";
import { geneLinks } from "../gene-annotations/links";
import { explainER } from "./mockApi";

describe("deterministic gene links (Tier 1)", () => {
  it("builds the four external links from the symbol", () => {
    const links = geneLinks("ESR1");
    expect(links.map((l) => l.label)).toEqual(["GeneCards", "NCBI Gene", "UniProt", "Ensembl"]);
    expect(links[0].url).toContain("gene=ESR1");
    expect(links[1].url).toContain("ESR1"); // NCBI term is URL-encoded but contains the symbol
    expect(links[3].url).toContain("g=ESR1");
  });

  it("renders links with target=_blank rel=noopener noreferrer + the symbol in href", () => {
    render(<GeneAnnotation gene="ESR1" />);
    const gc = screen.getByRole("link", { name: "GeneCards" });
    expect(gc).toHaveAttribute("target", "_blank");
    expect(gc).toHaveAttribute("rel", "noopener noreferrer");
    expect(gc.getAttribute("href")).toContain("ESR1");
  });

  it("shows 'annotation unavailable' when the artifact has no entry (no fabricated text)", () => {
    render(<GeneAnnotation gene="ESR1" />);
    expect(screen.getByText("annotation unavailable")).toBeInTheDocument();
    // No sourced-description attribution appears when there is no real entry.
    expect(screen.queryByText(/source:/i)).not.toBeInTheDocument();
  });
});

describe("sourced gene descriptions (Tier 2, real NCBI Gene artifact)", () => {
  it("a covered gene shows its sourced description + attribution (never fabricated)", () => {
    // BAMBI is one of the 305 landmark genes with a real committed NCBI Gene description.
    render(<GeneAnnotation gene="BAMBI" />);
    expect(screen.getByText(/source: NCBI Gene/i)).toBeInTheDocument();
    expect(screen.queryByText("annotation unavailable")).not.toBeInTheDocument();
  });
});

describe("gene contribution cards carry annotations + the honesty caption", () => {
  it("each contributor shows links + EITHER a sourced description OR 'annotation unavailable'", () => {
    render(<GeneContributionCards explanation={explainER as unknown as ExplanationResult} />);
    const n = (explainER as unknown as ExplanationResult).top_contributors.length;
    // Every gene has deterministic links.
    expect(screen.getAllByRole("link", { name: "NCBI Gene" })).toHaveLength(n);
    // Each gene is EITHER described-with-attribution OR "annotation unavailable" — never blank,
    // never fabricated. The two states partition the contributors exactly.
    const described = screen.queryAllByText(/source: NCBI Gene/i).length;
    const unavailable = screen.queryAllByText("annotation unavailable").length;
    expect(described + unavailable).toBe(n);
    expect(described).toBeGreaterThan(0); // BAMBI is covered in this fixture
    expect(screen.getByText(/not biological causality or regulatory validation/i)).toBeInTheDocument();
  });
});
