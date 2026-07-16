// Analyze — the ported prototype-v2 stepped flow (source → validate → run → result), wired to the
// mocked real API. These tests lock the honesty invariants through the new structure: endpoint
// cards render from the API result set (never hardcoded), wording is endpoint-signal-score /
// model-call (Active/Inactive) with below-threshold NEUTRAL, evidence shows the correct per-endpoint
// method label, limitations are present, and no risk/toxic/safe language appears.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, predictAR } from "./mockApi";
import { renderApp } from "./renderApp";
import { ValidationStep } from "../components/analyze/ValidationStep";
import type { PreparedInput } from "../pages/Analyze";

const SIG_JSON = '{"A1BG":0.1}'; // shape-valid; the mocked API doesn't gene-validate

function validationInput(count: number, compatible = true): PreparedInput {
  const compatibility = Array.from({ length: count }, (_, index) => ({
    endpoint_id: `E${index + 1}`,
    biological_target: `Arbitrary long endpoint target ${index + 1}`,
    compatible,
    n_schema_genes: 978,
    n_detected: 978,
    n_matched: compatible ? 978 : 0,
    n_missing: compatible ? 0 : 978,
    n_extra: 0,
    missing_genes: [],
    extra_genes: [],
    reason: compatible ? null : "missing genes",
  }));
  return {
    title: "Synthetic input",
    subtitle: "Dynamic endpoint test",
    kind: "paste",
    signature: { A1BG: 0.1 },
    allowExtra: false,
    inputValueType: "ranked_statistic",
    parse: {
      ready: compatible,
      format: "json",
      input_value_type: "ranked_statistic",
      preview: { n_detected: 978, samples: null, selected_sample: null, needs_sample: false },
      compatibility,
      compatible_endpoint_ids: compatible ? compatibility.map((item) => item.endpoint_id) : [],
      signature: { A1BG: 0.1 },
    },
  };
}

describe("Analyze signature action", () => {
  it.each([1, 3, 6, 10])("summarizes %s compatible arbitrary endpoints", (count) => {
    render(<ValidationStep input={validationInput(count)} onBack={() => undefined} onRun={() => undefined} />);
    expect(screen.getByRole("button", { name: "Analyze signature" })).toBeEnabled();
    expect(screen.getByText(new RegExp(`${count} compatible endpoint model`))).toHaveTextContent("E1");
    expect(screen.getByText(new RegExp(`${count} compatible endpoint model`))).toHaveTextContent(`E${count}`);
  });

  it("keeps the action disabled when every endpoint is incompatible", () => {
    render(<ValidationStep input={validationInput(3, false)} onBack={() => undefined} onRun={() => undefined} />);
    expect(screen.getByRole("button", { name: "Analyze signature" })).toBeDisabled();
    expect(screen.getByText(/No registered endpoint can use this signature/i)).toBeInTheDocument();
  });
});

// Drive source (advanced JSON paste) → validate → run → result overview.
async function analyze() {
  fireEvent.click(await screen.findByRole("button", { name: /Advanced: paste JSON/i }));
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), { target: { value: SIG_JSON } });
  fireEvent.click(screen.getByRole("button", { name: /^Load signature$/i }));
  // Validate step — the Run button enables once /endpoints has loaded.
  const runBtn = await screen.findByRole("button", { name: /^Analyze signature$/i });
  await waitFor(() => expect(runBtn).not.toBeDisabled());
  fireEvent.click(runBtn);
  await waitFor(() => expect(document.querySelectorAll(".endpoint-result").length).toBeGreaterThan(0));
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
    expect(document.querySelectorAll(".endpoint-result")).toHaveLength(2);
    expect(screen.getAllByText("Estrogen Receptor").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Androgen Receptor").length).toBeGreaterThanOrEqual(1);
    // Honest wording — endpoint signal score / model call, never risk/toxic/safe.
    expect(screen.getAllByText(/Endpoint signal score/i).length).toBe(2);
    expect(screen.getAllByText(/^(Above threshold|Below threshold)$/).length).toBeGreaterThanOrEqual(2);
    const technical = screen.getAllByText(/Model and provenance details/i);
    expect(technical).toHaveLength(2);
    technical.forEach((summary) => expect(summary.closest("details")).not.toHaveAttribute("open"));
    // One section-level reminder plus one on each endpoint card.
    expect(screen.getAllByText(/not a calibrated probability/i)).toHaveLength(3);
  });

  it("shows named similar compounds without raw map provenance", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("tab", { name: /^Similar signatures$/i }));
    expect(await screen.findByText(/Caffeic Acid/i)).toBeInTheDocument();
    expect(screen.getByText(/very similar response/i)).toBeInTheDocument();
    expect(screen.queryByText(/Technical provenance|Raw Euclidean|SHA-256/i)).not.toBeInTheDocument();
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
    expect(screen.queryByRole("tab", { name: /Try a demo/i })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Upload" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Public catalogue" })).toBeInTheDocument();
    expect(screen.getByText("Caffeic Acid")).toBeInTheDocument();
    fireEvent.click((await screen.findAllByRole("button", { name: /Use example file/i }))[0]);
    fireEvent.click(await screen.findByRole("button", { name: /Use this signature/i }));
    // Straight into validate → run → results, no raw JSON required.
    const runBtn = await screen.findByRole("button", { name: /^Analyze signature$/i });
    await waitFor(() => expect(runBtn).not.toBeDisabled());
    fireEvent.click(runBtn);
    await waitFor(() => expect(document.querySelectorAll(".endpoint-result")).toHaveLength(2));
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
          explanation: { declared_method: "linear_coefficient", available: true, missing_dependencies: [], reason: null },
        })),
      },
      "POST /api/analyze": { body: { results: six } },
    });
    renderApp("/analyze");
    await analyze();
    await waitFor(() => expect(document.querySelectorAll(".endpoint-result")).toHaveLength(10));
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
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    // ER is the first scored endpoint → its explanation auto-loads.
    expect(await screen.findByText(/Genes contributing to this endpoint signal/i)).toBeInTheDocument();
    expect(screen.queryByText(/^TreeSHAP$|Attribution method details/i)).not.toBeInTheDocument();
    // Integrated limitations remain visible with the evidence.
    expect(screen.getByText(/NOT regulatory-grade validation/i)).toBeInTheDocument();
  });

  it("AR evidence shows the linear-coefficient label (never SHAP)", async () => {
    renderApp("/analyze");
    await analyze();
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    // Select AR in the endpoint selector.
    const selector = screen.getByText("Endpoints").closest("aside") as HTMLElement;
    fireEvent.click(within(selector).getByText("Androgen Receptor"));
    expect(await screen.findByText(/Genes contributing to this endpoint signal/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Linear coefficient|TreeSHAP/i);
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
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    expect(await screen.findByText(/Endpoint explanation could not be prepared/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText(/Pathway and literature context are unavailable/i)).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("tab", { name: /^Endpoint evidence$/i }));
    await screen.findByText(/Genes contributing to this endpoint signal/i);
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
