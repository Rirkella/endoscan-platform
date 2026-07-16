import { fireEvent, render, screen } from "@testing-library/react";
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

  it("shows the exact pathway threshold state while retaining signed gene values", async () => {
    installFetchMock({
      "POST /api/interpret/biological-response": {
        body: { status: "ok", reason: null, input_value_type: "differential_zscore", increased_pathways: [{ pathway_id: "x", name: "Unsupported", direction: "increased", enrichment_statistic: 1, p_value: 0.5, q_value: 1, leading_edge_genes: [], pathway_size_in_universe: 10, statistically_supported: false }], decreased_pathways: [], tested_gene_universe: [], method_block: null },
      },
    });
    render(<BiologicalResponsePanel signature={{ ESR1: 0.1 }} inputValueType="differential_zscore" />);
    expect(await screen.findByText(/No Reactome pathway reached the current FDR threshold for this signature/i)).toBeInTheDocument();
    expect(screen.getByText(/Individual genes may still show differential values/i)).toBeInTheDocument();
    expect(screen.queryByText("Unsupported")).not.toBeInTheDocument();
    expect(screen.queryByText(/no gene response|normal range|biologically inactive/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Gene-level response/i }));
    expect(screen.getByText(/Strongest positive differential values/i)).toBeInTheDocument();
    expect(screen.getByText("+0.1000")).toBeInTheDocument();
    expect(screen.getByText(/not gene-level statistical significance/i)).toBeInTheDocument();
  });
});
