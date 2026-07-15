// Analyze — the ported prototype-v2 stepped flow (source → validate → run → result), wired to the
// mocked real API. These tests lock the honesty invariants through the new structure: endpoint
// cards render from the API result set (never hardcoded), wording is endpoint-signal-score /
// model-call (Active/Inactive) with below-threshold NEUTRAL, evidence shows the correct per-endpoint
// method label, limitations are present, and no risk/toxic/safe language appears.

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, predictAR } from "./mockApi";
import { renderApp } from "./renderApp";

const SIG_JSON = '{"A1BG":0.1}'; // shape-valid; the mocked API doesn't gene-validate

// Drive source (advanced JSON paste) → validate → run → result overview.
async function analyze() {
  fireEvent.click(await screen.findByRole("button", { name: /Advanced: paste JSON/i }));
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), { target: { value: SIG_JSON } });
  fireEvent.click(screen.getByRole("button", { name: /^Load signature$/i }));
  // Validate step — the Run button enables once /endpoints has loaded.
  const runBtn = await screen.findByRole("button", { name: /Run \d+ compatible endpoint model/i });
  await waitFor(() => expect(runBtn).not.toBeDisabled());
  fireEvent.click(runBtn);
  await screen.findAllByRole("article"); // result overview cards
}

describe("Analyze stepped flow + result overview", () => {
  beforeEach(() => installFetchMock());

  it("source step renders with an enabled upload and JSON is only a secondary action", async () => {
    renderApp("/analyze");
    expect(
      await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i }),
    ).toBeInTheDocument();
    // Upload is the primary entry and enabled (not a disabled placeholder).
    expect(screen.getByText(/Upload a signature file/i)).toBeInTheDocument();
    const fileIn = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileIn).not.toBeDisabled();
    // JSON paste is NOT the primary visual entry — it hides behind the advanced disclosure.
    expect(screen.queryByLabelText(/Signature \(JSON/i)).not.toBeInTheDocument();
  });

  it("overview cards render from the mocked API (ER + AR), not hardcoded", async () => {
    renderApp("/analyze");
    await analyze();
    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(2);
    expect(screen.getAllByText("Estrogen Receptor").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Androgen Receptor").length).toBeGreaterThanOrEqual(1);
    // Honest wording — endpoint signal score / model call, never risk/toxic/safe.
    expect(screen.getAllByText(/Endpoint signal score/i).length).toBe(2);
    expect(screen.getAllByText(/Model call/i).length).toBe(2);
    expect(screen.getAllByText(/^(Above threshold|Below threshold)$/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Model version/i)).toHaveLength(2);
    expect(screen.getByText(/lincs_gse92742, toxcast_er/i)).toBeInTheDocument();
    expect(screen.getAllByText(/978 \/ 978 required genes/i).length).toBeGreaterThanOrEqual(1);
  });

  it("shows reference distances and map provenance for the submitted signature", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("button", { name: /^Reference context$/i }));
    expect(await screen.findByText(/Distance 0\.420/i)).toBeInTheDocument();
    expect(screen.getByText(/Reference data provenance/i)).toBeInTheDocument();
  });

  it("below-threshold is NEUTRAL, not green/safe (fixture: both endpoints below threshold)", async () => {
    renderApp("/analyze");
    await analyze();
    // Both fixture endpoints are below threshold → neutral wording, never a safety label.
    expect(screen.getAllByText(/Below threshold/i).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/No endpoint model is above its threshold/i)).toBeInTheDocument();
    // No reassuring/hazard language anywhere ("safety conclusion" is allowed; a bare "safe" is not).
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\bsafe\b/i);
    expect(text).not.toMatch(/low risk/i);
    expect(text).not.toMatch(/risk score/i);
  });

  it("a bundled REAL demo signature completes the whole Analyze flow (no JSON copying)", async () => {
    renderApp("/analyze");
    // Open the demo tab and run the first bundled real demo with one click.
    fireEvent.click(await screen.findByRole("tab", { name: /Try a demo/i }));
    const demoButtons = await screen.findAllByRole("button", { name: /Use this demo/i });
    expect(demoButtons.length).toBeGreaterThan(0); // real demos are bundled
    fireEvent.click(demoButtons[0]);
    // Straight into validate → run → results, no raw JSON required.
    const runBtn = await screen.findByRole("button", { name: /Run \d+ compatible endpoint model/i });
    await waitFor(() => expect(runBtn).not.toBeDisabled());
    fireEvent.click(runBtn);
    expect((await screen.findAllByRole("article")).length).toBe(2);
  });

  it("renders one card per API result — scales to 10 endpoints without a fixed layout", async () => {
    const six = Array.from({ length: 10 }, (_, i) => ({
      endpoint_id: `E${i + 1}`,
      biological_target: `Target ${i + 1}`,
      ok: true,
      result: { ...predictAR, endpoint_id: `E${i + 1}` },
      error: null,
    }));
    installFetchMock({
      "GET /api/endpoints": {
        body: six.map((e) => ({
          endpoint_id: e.endpoint_id,
          biological_target: e.biological_target,
          status: "experimental",
          input_type: "transcriptomics",
          frozen: false,
        })),
      },
      "POST /api/analyze": { body: { results: six } },
    });
    renderApp("/analyze");
    await analyze();
    expect(await screen.findAllByRole("article")).toHaveLength(10); // one per API result
  });

  it("shows a dedicated all-failed state with isolated endpoint cards", async () => {
    const failures = ["ER", "AR"].map((endpoint_id) => ({
      endpoint_id,
      biological_target: endpoint_id === "ER" ? "Estrogen Receptor" : "Androgen Receptor",
      ok: false,
      result: null,
      error: {
        error: "model_unavailable",
        detail: "The registered model is temporarily unavailable.",
        endpoint_id,
        request_id: "req-all-failed",
      },
    }));
    installFetchMock({
      "POST /api/analyze": {
        body: {
          results: failures,
          summary: { requested: 2, succeeded: 0, failed: 2, status: "all_failed" },
        },
      },
    });
    renderApp("/analyze");
    await analyze();
    expect(
      screen.getByText(/No compatible endpoint completed successfully/i),
    ).toBeInTheDocument();
    expect(await screen.findAllByRole("article")).toHaveLength(2);
  });
});

describe("evidence tab — correct method label per endpoint", () => {
  beforeEach(() => installFetchMock());

  it("ER evidence shows TreeSHAP + real limitations", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("button", { name: /^Evidence$/i }));
    // ER is the first scored endpoint → its explanation auto-loads.
    expect(await screen.findByText(/Method:/i)).toHaveTextContent("TreeSHAP");
    // Integrated limitations remain visible with the evidence.
    expect(screen.getByText(/NOT regulatory-grade validation/i)).toBeInTheDocument();
  });

  it("AR evidence shows the linear-coefficient label (never SHAP)", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("button", { name: /^Evidence$/i }));
    // Select AR in the endpoint selector.
    const selector = screen.getByText("Endpoints").closest("aside") as HTMLElement;
    fireEvent.click(within(selector).getByText("Androgen Receptor"));
    expect(
      await screen.findByText(
        /Linear coefficient attribution \(coefficient × value — not a SHAP value\)/i,
      ),
    ).toBeInTheDocument();
  });

  it("explain 501 renders a clean, non-scary state (live API drives availability)", async () => {
    installFetchMock({
      "POST /api/explain": {
        status: 501,
        body: {
          error: "explain_unsupported_for_model",
          detail: "no attributor for model type 'Foo'",
          endpoint_id: "ER",
        },
      },
    });
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("button", { name: /^Evidence$/i }));
    expect(
      await screen.findByText(/aren.t available for this endpoint.s model type/i),
    ).toBeInTheDocument();
  });
});

describe("integrated honesty framing", () => {
  beforeEach(() => installFetchMock());

  it("the experimental status + non-diagnostic disclaimer are present, no overclaiming copy", async () => {
    renderApp("/analyze");
    await analyze();
    // Persistent experimental status in the shell (topbar note) + the non-diagnostic disclaimer.
    expect(screen.getByRole("note")).toHaveTextContent(/Experimental models/i);
    expect(
      screen.getByText(/not a regulatory, clinical, or diagnostic tool/i),
    ).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    for (const bad of [/risk score/i, /\btoxic\b/i, /\bis safe\b/i, /confirmed EDC/i]) {
      expect(text).not.toMatch(bad);
    }
  });

  it("limitations (status + CI reason + scope + disclaimer) are readable in the evidence tab", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("button", { name: /^Evidence$/i }));
    await screen.findByText(/Method:/i); // ER evidence loaded
    expect(screen.getAllByTitle(/Model status: experimental/i).length).toBeGreaterThan(0);
    expect(
      screen.getByText(/auroc 95% CI lower bound 0.675 < 0.75 floor/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Scope of claim/i)).toBeInTheDocument();
    expect(screen.getByText(/NOT regulatory-grade validation/i)).toBeInTheDocument();
  });
});

describe("Model Library (rendered from the API)", () => {
  beforeEach(() => installFetchMock());

  it("lists endpoints from the API at /library", async () => {
    renderApp("/library");
    expect(await screen.findByText("Estrogen Receptor")).toBeInTheDocument();
    expect(screen.getByText("Androgen Receptor")).toBeInTheDocument();
    expect(screen.getAllByTitle(/Model status: experimental/i).length).toBeGreaterThanOrEqual(2);
  });

  it("evidence page renders limitations + a single-variant list at /library/:id", async () => {
    renderApp("/library/AR");
    expect(
      await screen.findByText("auprc 95% CI lower bound 0.420 < 0.50 floor"),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Scope of claim/i).length).toBeGreaterThan(0);
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument(); // one variant today
  });
});
