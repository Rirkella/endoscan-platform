import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExplanationResult } from "../api/types";
import { GeneAnnotation } from "../components/GeneAnnotation";
import { GeneContributionCards } from "../components/GeneContributionCards";
import { geneLinks } from "../gene-annotations/links";
import { explainER } from "./mockApi";

describe("deterministic gene resources", () => {
  it("builds external links from the gene symbol", () => {
    const links = geneLinks("ESR1");
    expect(links.map((item) => item.label)).toEqual(["GeneCards", "NCBI Gene", "UniProt", "Ensembl"]);
    expect(links.every((item) => item.url.includes("ESR1"))).toBe(true);
  });

  it("keeps safe external links and omits unavailable annotation copy", () => {
    render(<GeneAnnotation gene="ESR1" />);
    const link = screen.getByRole("link", { name: "NCBI Gene" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByText("annotation unavailable")).not.toBeInTheDocument();
  });

  it("shows a committed sourced annotation when one exists", () => {
    render(<GeneAnnotation gene="BAMBI" />);
    expect(screen.getByText(/source: NCBI Gene/i)).toBeInTheDocument();
  });
});

describe("gene contribution visualization", () => {
  it("separates direction, ranks magnitude, and keeps annotations collapsed", () => {
    render(<GeneContributionCards explanation={explainER as unknown as ExplanationResult} />);
    const n = (explainER as unknown as ExplanationResult).top_contributors.length;
    expect(screen.getByText("Increases this model’s signal")).toBeInTheDocument();
    expect(screen.getByText("Decreases this model’s signal")).toBeInTheDocument();
    expect(screen.queryByText("annotation unavailable")).not.toBeInTheDocument();
    screen.getAllByText("Gene details").forEach((summary) => {
      expect(summary.closest("details")).not.toHaveAttribute("open");
    });
    expect(document.querySelectorAll(".contribution-bar span")).toHaveLength(n);
    expect(screen.getByText(/Ranked by absolute model contribution/i)).toBeInTheDocument();
  });
});
