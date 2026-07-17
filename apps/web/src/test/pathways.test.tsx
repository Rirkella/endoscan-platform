import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { PathwaysPanel } from "../components/PathwaysPanel";
import { installFetchMock, pathwaysOk } from "./mockApi";

const SIG = { DDR1: 0.1 };

describe("Endpoint-specific pathways", () => {
  beforeEach(() => installFetchMock());

  it("shows supported pathways concisely without technical panels", async () => {
    installFetchMock({ "POST /api/interpret/pathways": { body: pathwaysOk } });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    expect(await screen.findByText(/Signaling by Nuclear Receptors/i)).toBeInTheDocument();
    expect(screen.getByText(/Associated genes: ESR1, GREB1, PGR/i)).toBeInTheDocument();
    expect(screen.getAllByText(/FDR/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Technical details|Method and technical details|Reactome license/i)).not.toBeInTheDocument();
  });

  it("hides q=1 exploratory broad pathways", async () => {
    installFetchMock({
      "POST /api/interpret/pathways": {
        body: {
          ...pathwaysOk,
          pathways: [],
          exploratory_pathways: [
            { ...pathwaysOk.pathways[0], pathway_id: "broad", name: "Disease", q_value: 1 },
          ],
        },
      },
    });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    expect(await screen.findByText(/No endpoint-specific pathway association reached the current evidence threshold/i)).toBeInTheDocument();
    expect(screen.queryByText("Disease")).not.toBeInTheDocument();
  });

  it.each(["unavailable", "too_few_genes", "ok"])("uses one concise empty state for %s", async (status) => {
    installFetchMock({
      "POST /api/interpret/pathways": {
        body: { endpoint_id: "ER", method: "tree_shap", status, reason: "x", pathways: [], exploratory_pathways: [], method_block: null },
      },
    });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await waitFor(() => expect(screen.getByText(/No endpoint-specific pathway association reached the current evidence threshold/i)).toBeInTheDocument());
  });
});
