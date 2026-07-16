import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { BiologicalResponsePanel } from "../components/analyze/BiologicalResponsePanel";
import { installFetchMock } from "./mockApi";

describe("Full-signature transcriptomic response", () => {
  beforeEach(() => installFetchMock());

  it("shows FDR-supported positive and negative ranked enrichment with accurate labels", async () => {
    render(<BiologicalResponsePanel signature={{ ESR1: 2, CDK1: -2 }} inputValueType="differential_zscore" />);
    expect(await screen.findByText("Pathways enriched among genes with increased expression")).toBeInTheDocument();
    expect(screen.getByText("Pathways enriched among genes with decreased expression")).toBeInTheDocument();
    expect(screen.getByText("Estrogen-dependent gene expression")).toBeInTheDocument();
    expect(screen.getByText("Cell cycle checkpoints")).toBeInTheDocument();
    expect(screen.getAllByText(/Enrichment score/i).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/FDR/i).length).toBeGreaterThan(1);
    expect(screen.queryByText(/Method and technical details|healthy reference|normal healthy/i)).not.toBeInTheDocument();
  });

  it("does not infer direction from raw expression without a matched reference", async () => {
    installFetchMock({
      "POST /api/interpret/biological-response": {
        body: { status: "unsupported_input", reason: "x", input_value_type: "raw_expression", increased_pathways: [], decreased_pathways: [], tested_gene_universe: [], method_block: null },
      },
    });
    render(<BiologicalResponsePanel signature={{ ESR1: 2 }} inputValueType="raw_expression" />);
    expect(await screen.findAllByText(/requires a differential signature or matched control/i)).not.toHaveLength(0);
    expect(screen.queryByText(/Pathways enriched among genes with increased expression/i)).not.toBeInTheDocument();
  });

  it("shows a concise low-response state and hides unsupported tendencies", async () => {
    installFetchMock({
      "POST /api/interpret/biological-response": {
        body: { status: "ok", reason: null, input_value_type: "differential_zscore", increased_pathways: [{ pathway_id: "x", name: "Unsupported", direction: "increased", enrichment_statistic: 1, p_value: 0.5, q_value: 1, leading_edge_genes: [], pathway_size_in_universe: 10, statistically_supported: false }], decreased_pathways: [], tested_gene_universe: [], method_block: null },
      },
    });
    render(<BiologicalResponsePanel signature={{ ESR1: 0.1 }} inputValueType="differential_zscore" />);
    expect(await screen.findByText(/No strong differential response was detected relative to the matched experimental control/i)).toBeInTheDocument();
    expect(screen.queryByText("Unsupported")).not.toBeInTheDocument();
  });
});
