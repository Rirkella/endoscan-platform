import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExplanationResult } from "../api/types";
import { GeneAnnotation } from "../components/GeneAnnotation";
import { GeneContributionCards } from "../components/GeneContributionCards";
import { geneLinks } from "../gene-annotations/links";
import { explainER } from "./mockApi";

describe("deterministic gene resources", () => {
  it("builds safe external links from the gene symbol", () => {
    const links = geneLinks("ESR1");
    expect(links.map((item) => item.label)).toEqual(["GeneCards", "NCBI Gene", "UniProt", "Ensembl"]);
    expect(links.every((item) => item.url.includes("ESR1"))).toBe(true);
  });

  it("omits unavailable annotation copy and shows sourced copy when present", () => {
    const { rerender } = render(<GeneAnnotation gene="ESR1" />);
    expect(screen.getByRole("link", { name: "NCBI Gene" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByText("annotation unavailable")).not.toBeInTheDocument();
    rerender(<GeneAnnotation gene="BAMBI" />);
    expect(screen.getByText(/source: NCBI Gene/i)).toBeInTheDocument();
  });
});

describe("gene contribution visualization", () => {
  it("uses one absolute-magnitude ranking with signed values and directions", () => {
    const explanation = explainER as unknown as ExplanationResult;
    render(<GeneContributionCards explanation={explanation} />);
    const items = Array.from(document.querySelectorAll(".contribution-ranking > li"));
    expect(items).toHaveLength(explanation.top_contributors.length);
    const expected = [...explanation.top_contributors].sort((a, b) => Math.abs(b.shap_value) - Math.abs(a.shap_value));
    items.forEach((item, index) => {
      expect(item).toHaveTextContent(String(index + 1));
      expect(item).toHaveTextContent(expected[index].gene);
      expect(item).toHaveTextContent(expected[index].shap_value.toFixed(4));
      expect(item).toHaveTextContent(expected[index].shap_value >= 0 ? "increases score" : "decreases score");
    });
    expect(document.querySelectorAll(".diverging-contribution")).toHaveLength(expected.length);
    expect(screen.queryByText(/Attribution method details|annotation unavailable/i)).not.toBeInTheDocument();
    document.querySelectorAll(".gene-details").forEach((details) => expect(details).not.toHaveAttribute("open"));
  });
});
