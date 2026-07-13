// Phase 2 — signature upload wiring. The upload zone is enabled; the SERVER is the one gene
// validator (frontend does NO independent gene check); errors are shown verbatim.

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installFetchMock, parseInvalid } from "./mockApi";
import { renderApp } from "./renderApp";

function fileInput(): HTMLInputElement {
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

function uploadCsv(name = "sig.csv") {
  const file = new File(["gene,value\nA1BG,0.1"], name, { type: "text/csv" });
  fireEvent.change(fileInput(), { target: { files: [file] } });
}

describe("signature upload", () => {
  beforeEach(() => installFetchMock());

  it("the upload zone is enabled (not the Phase-1 disabled placeholder)", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a signature/i });
    expect(fileInput()).not.toBeDisabled();
  });

  it("upload CSV → real preview → analyze → score-card grid", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a signature/i });
    uploadCsv();
    // Preview reflects the real parse result (n_matched from the API, not fabricated).
    expect(await screen.findByText(/Aligned to 978 schema genes/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Use this signature/i }));
    const analyzeBtn = await screen.findByRole("button", { name: /Analyze across endpoints/i });
    await waitFor(() => expect(analyzeBtn).not.toBeDisabled());
    fireEvent.click(analyzeBtn);
    expect((await screen.findAllByRole("article")).length).toBe(2); // grid from /analyze
  });

  it("invalid upload shows the API's gene-level message VERBATIM", async () => {
    installFetchMock({ "POST /api/signatures/parse": { status: 422, body: parseInvalid } });
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a signature/i });
    uploadCsv();
    expect(await screen.findByText(parseInvalid.detail)).toBeInTheDocument();
  });

  it("the frontend does NO independent gene validation — it calls the API to validate", async () => {
    installFetchMock();
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a signature/i });
    uploadCsv();
    await screen.findByText(/Aligned to 978 schema genes/i);
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    const calledParse = fetchMock.mock.calls.some((c) => String(c[0]).includes("/signatures/parse"));
    expect(calledParse).toBe(true); // validation is delegated to the server, always
  });

  it("demo picker + JSON paste still work alongside upload", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a signature/i });
    // JSON paste path
    fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
      target: { value: '{"A1BG":0.1}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));
    const analyzeBtn = await screen.findByRole("button", { name: /Analyze across endpoints/i });
    await waitFor(() => expect(analyzeBtn).not.toBeDisabled());
    fireEvent.click(analyzeBtn);
    expect((await screen.findAllByRole("article")).length).toBe(2);
    // demo picker present
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });
});
