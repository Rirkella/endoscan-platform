import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

async function loadSignature() {
  // Endpoints must be loaded first (selectedId defaults to the first endpoint).
  await screen.findByRole("option", { name: /Estrogen Receptor/i });
  const textarea = screen.getByLabelText(/Signature \(JSON/i);
  fireEvent.change(textarea, { target: { value: '{"A1BG":0.1}' } });
  fireEvent.click(screen.getByRole("button", { name: /Use this signature/i }));
}

describe("analyze: predict + explain + clean error states", () => {
  beforeEach(() => installFetchMock());

  it("predict renders probability, threshold, and the limitations block", async () => {
    renderApp("/analyze");
    await loadSignature();
    fireEvent.click(await screen.findByRole("button", { name: "Predict" }));
    expect(await screen.findByText(/Probability score/i)).toBeInTheDocument();
    expect(screen.getByText(/threshold/i)).toBeInTheDocument();
    // honesty rides on the result:
    expect(screen.getByText(/not a probability of real-world toxicity/i)).toBeInTheDocument();
    expect(screen.getByText(/NOT regulatory-grade validation/i)).toBeInTheDocument();
  });

  it("explain on ER shows the TreeSHAP method label", async () => {
    renderApp("/analyze");
    await loadSignature(); // ER is the default (first) endpoint
    fireEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(await screen.findByText("TreeSHAP")).toBeInTheDocument();
    expect(screen.getByText(/Top gene contributions/i)).toBeInTheDocument();
  });

  it("explain on AR shows the linear-coefficient method label (not SHAP)", async () => {
    renderApp("/analyze");
    await screen.findByRole("option", { name: /Androgen Receptor/i });
    fireEvent.change(screen.getByLabelText(/^Endpoint$/i), { target: { value: "AR" } });
    await loadSignature();
    fireEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(
      await screen.findByText(/Linear coefficient attribution \(coefficient × value — not a SHAP value\)/i),
    ).toBeInTheDocument();
  });

  it("gene-mismatch predict renders the API's 422 message verbatim", async () => {
    installFetchMock({
      "POST /api/predict": {
        status: 422,
        body: {
          error: "invalid_signature",
          detail: "signature missing 3 of 978 schema genes (first few: ['A1BG'])",
          endpoint_id: "ER",
        },
      },
    });
    renderApp("/analyze");
    await loadSignature();
    fireEvent.click(await screen.findByRole("button", { name: "Predict" }));
    expect(await screen.findByText(/didn.t match the endpoint.s gene schema/i)).toBeInTheDocument();
    expect(screen.getByText(/missing 3 of 978 schema genes/i)).toBeInTheDocument();
  });

  it("explain 501 renders a calm, non-scary 'not available' state", async () => {
    installFetchMock({
      "POST /api/explain": {
        status: 501,
        body: {
          error: "explain_unsupported_for_model",
          detail: "no implemented attributor for model type 'Foo'",
          endpoint_id: "ER",
        },
      },
    });
    renderApp("/analyze");
    await loadSignature();
    fireEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/aren.t available for this endpoint.s model type/i)).toBeInTheDocument();
  });

  it("explain 503 renders a calm 'temporarily unavailable' state", async () => {
    installFetchMock({
      "POST /api/explain": {
        status: 503,
        body: {
          error: "explain_unavailable",
          detail: "TreeSHAP explainability requires the 'explain' extra (shap)",
          endpoint_id: "ER",
        },
      },
    });
    renderApp("/analyze");
    await loadSignature();
    fireEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument();
  });
});
