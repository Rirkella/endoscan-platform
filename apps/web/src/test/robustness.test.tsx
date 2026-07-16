// Dynamic-endpoint robustness. The frontend must render whatever /endpoints and /analyze return —
// 1, 3, 6+ endpoints, arbitrary/long IDs, and any mix of available data — without ER/AR-specific
// layout logic and without crashing on missing/sparse data. Each case mocks a bespoke API shape.

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { installFetchMock, predictAR, explainER } from "./mockApi";
import { renderApp } from "./renderApp";

interface Ep {
  endpoint_id: string;
  biological_target: string;
}

function endpointsBody(eps: Ep[]) {
  return eps.map((e) => ({
    endpoint_id: e.endpoint_id,
    biological_target: e.biological_target,
    status: "experimental",
    input_type: "transcriptomics",
    frozen: false,
    explanation: { declared_method: "linear_coefficient", available: true, missing_dependencies: [], reason: null },
  }));
}

function okEntry(e: Ep) {
  return {
    endpoint_id: e.endpoint_id,
    biological_target: e.biological_target,
    ok: true,
    result: {
      ...predictAR,
      endpoint_id: e.endpoint_id,
      limitations: { ...predictAR.limitations, endpoint_id: e.endpoint_id },
    },
    error: null,
  };
}

function errEntry(e: Ep) {
  return {
    endpoint_id: e.endpoint_id,
    biological_target: e.biological_target,
    ok: false,
    result: null,
    error: { error: "invalid_signature", detail: "incompatible input for this endpoint", endpoint_id: e.endpoint_id },
  };
}

// Install a mock whose /endpoints and /analyze are built from an arbitrary endpoint set.
function installFor(eps: Ep[], entries: object[], extra: Record<string, { status?: number; body: unknown }> = {}) {
  installFetchMock({
    "GET /api/endpoints": { body: endpointsBody(eps) },
    "POST /api/analyze": { body: { results: entries } },
    ...extra,
  });
}

// Paste a shape-valid signature and run through to the result overview.
async function runAnalyze() {
  fireEvent.click(await screen.findByRole("button", { name: /Advanced: paste JSON/i }));
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), { target: { value: '{"A1BG":0.1}' } });
  fireEvent.click(screen.getByRole("button", { name: /^Load signature$/i }));
  const runBtn = await screen.findByRole("button", { name: /^Analyze signature$/i });
  await waitFor(() => expect(runBtn).not.toBeDisabled());
  fireEvent.click(runBtn);
  await screen.findAllByRole("article");
}

describe("dynamic endpoint count — cards render from the API, no fixed layout", () => {
  for (const n of [1, 3, 6, 10]) {
    it(`${n} endpoint(s) → ${n} result card(s)`, async () => {
      const eps = Array.from({ length: n }, (_, i) => ({
        endpoint_id: `EP${i + 1}`,
        biological_target: `Target ${i + 1}`,
      }));
      installFor(eps, eps.map(okEntry));
      renderApp("/analyze");
      await runAnalyze();
      expect(await screen.findAllByRole("article")).toHaveLength(n);
    });
  }

  it("a long endpoint name and an arbitrary ID render without breaking the layout", async () => {
    const eps = [
      {
        endpoint_id: "AHR_ARYL_HYDROCARBON_RECEPTOR_LONGNAME_V2",
        biological_target: "Aryl hydrocarbon receptor activation (long descriptive endpoint name)",
      },
    ];
    installFor(eps, eps.map(okEntry));
    renderApp("/analyze");
    await runAnalyze();
    expect(
      screen.getAllByText(/Aryl hydrocarbon receptor activation/i).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole("article")).toHaveLength(1);
  });

  it("mixed compatible/incompatible endpoints → a result card AND a clean 'not available' card", async () => {
    const eps = [
      { endpoint_id: "OKEP", biological_target: "Compatible endpoint" },
      { endpoint_id: "BADEP", biological_target: "Incompatible endpoint" },
    ];
    installFor(eps, [okEntry(eps[0]), errEntry(eps[1])]);
    renderApp("/analyze");
    await runAnalyze();
    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(2); // both endpoints render; one failing does not sink the other
    expect(screen.getByText(/Not available/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Below threshold/i).length).toBeGreaterThan(0); // the OK one still scores
  });
});

describe("missing / sparse per-endpoint data does not crash the UI", () => {
  const eps = [{ endpoint_id: "SOLO", biological_target: "Solo endpoint" }];

  it("no pathways meeting the threshold → an honest empty state in evidence", async () => {
    installFor(eps, eps.map(okEntry), {
      "POST /api/explain": { body: explainER },
      "POST /api/interpret/pathways": {
        body: { endpoint_id: "SOLO", method: "tree_shap", status: "ok", reason: null, pathways: [], method_block: null },
      },
    });
    renderApp("/analyze");
    await runAnalyze();
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    expect(
      await screen.findByText(/No endpoint-specific pathway association reached the current evidence threshold/i),
    ).toBeInTheDocument();
  });

  it("no explanation available (501) → a calm 'not available' state in evidence", async () => {
    installFetchMock({
      "GET /api/endpoints": { body: endpointsBody(eps).map((endpoint) => ({ ...endpoint, explanation: { declared_method: "tree_shap", available: false, missing_dependencies: ["shap"], reason: "Missing runtime dependencies: shap." } })) },
      "POST /api/analyze": { body: { results: eps.map(okEntry) } },
    });
    renderApp("/analyze");
    await runAnalyze();
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    expect(
      await screen.findByText(/Endpoint explanation is not available/i),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText(/Pathway and literature context are unavailable/i)).toBeInTheDocument();
  });

  it("no reference map for the endpoint → an honest empty state in the reference tab", async () => {
    installFor(eps, eps.map(okEntry), {
      "GET /api/explore/SOLO/umap": { status: 404, body: { error: "not_computed", detail: "no map", endpoint_id: "SOLO" } },
    });
    renderApp("/analyze");
    await runAnalyze();
    fireEvent.click(screen.getByRole("tab", { name: /^Similar signatures$/i }));
    expect(await screen.findByText(/No reference map for SOLO yet/i)).toBeInTheDocument();
  });

  it("an unknown future model status renders as-is (no assumption, no crash)", async () => {
    const future = [{ endpoint_id: "FUT", biological_target: "Future endpoint" }];
    const entry = okEntry(future[0]);
    entry.result.limitations = { ...entry.result.limitations, status: "provisional_v3" };
    installFor(future, [entry]);
    renderApp("/analyze");
    await runAnalyze();
    // The unknown status string is shown verbatim; the card still renders.
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getAllByText(/provisional_v3/i).length).toBeGreaterThan(0);
  });
});
