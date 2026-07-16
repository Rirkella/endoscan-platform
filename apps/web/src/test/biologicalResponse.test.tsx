import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { BiologicalResponsePanel } from "../components/analyze/BiologicalResponsePanel";
import { installFetchMock } from "./mockApi";

describe("Global biological response", () => {
  beforeEach(() => installFetchMock());

  it("separates increased and decreased pathways with leading-edge genes and FDR", async () => {
    render(<BiologicalResponsePanel signature={{ ESR1: 2, CDK1: -2 }} inputValueType="differential_zscore" />);
    expect(await screen.findByText("Increased biological response")).toBeInTheDocument();
    expect(screen.getByText("Decreased biological response")).toBeInTheDocument();
    expect(screen.getByText("Estrogen-dependent gene expression")).toBeInTheDocument();
    expect(screen.getByText("Cell cycle checkpoints")).toBeInTheDocument();
    expect(screen.getByText("ESR1")).toBeInTheDocument();
    expect(screen.getAllByText(/FDR/i).length).toBeGreaterThan(1);
    const method = screen.getByText("Method and technical details").closest("details");
    expect(method).not.toHaveAttribute("open");
  });

  it("does not infer direction from raw expression without a matched reference", async () => {
    installFetchMock({
      "POST /api/interpret/biological-response": {
        body: {
          status: "unsupported_input",
          reason: "Raw expression values without a matched reference cannot be interpreted as increased or decreased pathway response.",
          input_value_type: "raw_expression",
          increased_pathways: [],
          decreased_pathways: [],
          tested_gene_universe: [],
          method_block: null,
        },
      },
    });
    render(<BiologicalResponsePanel signature={{ ESR1: 2 }} inputValueType="raw_expression" />);
    expect(await screen.findByText(/requires a differential or ranked signature|without a matched reference/i)).toBeInTheDocument();
    expect(screen.queryByText("Increased biological response")).not.toBeInTheDocument();
  });
});
