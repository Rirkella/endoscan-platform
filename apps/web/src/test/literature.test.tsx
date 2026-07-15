import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { LiteraturePanel } from "../components/LiteraturePanel";
import { installFetchMock } from "./mockApi";

const BASE = {
  endpoint_id: "ER",
  endpoint_name: "Estrogen receptor",
  reason: null,
  queries: [
    {
      category: "gene_endpoint",
      query: '"ESR1"[Title/Abstract] AND "Estrogen receptor"[Title/Abstract]',
      matched_genes: ["ESR1"],
      matched_pathways: [],
      pmids: ["12345"],
    },
  ],
  provenance: {
    provider: "NCBI PubMed E-utilities",
    database: "pubmed",
    eutils_base_url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    retrieved_at: "2026-07-15T00:00:00Z",
    tool: "endoscan",
    email_configured: true,
    api_key_used: false,
    rate_limit_per_second: 3,
    cache_hit: false,
  },
};

function renderPanel() {
  render(
    <LiteraturePanel
      endpointId="ER"
      genes={["ESR1"]}
      pathways={[{ pathway_id: "R-HSA-1", name: "Estrogen signaling", genes: ["ESR1"] }]}
    />,
  );
}

describe("Supporting literature", () => {
  beforeEach(() => installFetchMock());

  it("renders attributed PubMed records, external safety, and exact queries", async () => {
    installFetchMock({
      "POST /api/interpret/literature": {
        body: {
          ...BASE,
          status: "ok",
          articles: [
            {
              pmid: "12345",
              title: "Estrogen receptor transcription in human cells",
              authors: ["Example A"],
              journal: "Example Journal",
              year: "2024",
              abstract_excerpt: "Measured expression evidence.",
              matched_genes: ["ESR1"],
              matched_pathways: [],
              evidence_category: "gene_endpoint",
              relevance_reason: "Matched by the recorded query; not evidence of causality.",
              pubmed_url: "https://pubmed.ncbi.nlm.nih.gov/12345/",
            },
          ],
        },
      },
    });
    renderPanel();
    const link = await screen.findByRole("link", { name: /Estrogen receptor transcription/i });
    expect(link).toHaveAttribute("href", "https://pubmed.ncbi.nlm.nih.gov/12345/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getAllByText(/not proof of causality|not evidence of causality/i)).toHaveLength(2);
    expect(screen.getByTestId("literature-technical")).toHaveTextContent(/ESR1.*Estrogen receptor/i);
  });

  it.each([
    ["empty", "No PubMed records matched"],
    ["rate_limited", "request limit was reached"],
    ["timeout", "did not respond"],
  ])("renders the %s state without fabricated cards", async (status, reason) => {
    installFetchMock({
      "POST /api/interpret/literature": {
        body: { ...BASE, status, reason, articles: [] },
      },
    });
    renderPanel();
    await screen.findByText(reason);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
