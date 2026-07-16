// Signature upload wiring in the ported source step. Upload is the PRIMARY entry and enabled; the
// SERVER is the one gene validator (the frontend does NO independent gene check); parse errors are
// shown verbatim. JSON paste stays a secondary "advanced" action.

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installFetchMock, parseInvalid, parseOk } from "./mockApi";
import { renderApp } from "./renderApp";

function fileInput(): HTMLInputElement {
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

function uploadCsv(name = "sig.csv") {
  const file = new File(["gene,value\nA1BG,0.1"], name, { type: "text/csv" });
  fireEvent.change(fileInput(), { target: { files: [file] } });
}

describe("signature upload (source step)", () => {
  beforeEach(() => installFetchMock());

  it("the upload zone is the primary, enabled entry point", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    expect(screen.getByText(/Upload a signature file/i)).toBeInTheDocument();
    expect(fileInput()).not.toBeDisabled();
  });

  it("upload CSV → real coverage preview → use → validate → run → overview cards", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    uploadCsv();
    // Preview reflects endpoint-aware compatibility from the API, not a global schema.
    expect(await screen.findByText(/compatible endpoint models/i)).toBeInTheDocument();
    expect(screen.getByText(/978 genes parsed/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Use this signature/i }));
    const runBtn = await screen.findByRole("button", { name: /^Analyze signature$/i });
    await waitFor(() => expect(runBtn).not.toBeDisabled());
    fireEvent.click(runBtn);
    expect((await screen.findAllByRole("article")).length).toBe(2);
  });

  it("invalid upload shows the API's gene-level message VERBATIM", async () => {
    installFetchMock({ "POST /api/signatures/parse": { status: 422, body: parseInvalid } });
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    uploadCsv();
    expect(await screen.findByText(parseInvalid.detail)).toBeInTheDocument();
  });

  it("the frontend does NO independent gene validation — it calls the API to validate", async () => {
    installFetchMock();
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    uploadCsv();
    await screen.findByText(/compatible endpoint models/i);
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    const calledParse = fetchMock.mock.calls.some((c) =>
      String(c[0]).includes("/signatures/parse"),
    );
    expect(calledParse).toBe(true); // validation is delegated to the server, always
  });

  it("a real example CSV uses the same multipart parser path as a user upload", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    fireEvent.click(screen.getAllByRole("button", { name: /Use example file/i })[0]);
    expect(await screen.findByText(/compatible endpoint models/i)).toBeInTheDocument();
    const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
    const parseCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes("/signatures/parse"),
    );
    expect(parseCall).toBeDefined();
    const form = parseCall?.[1]?.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    const uploaded = form.get("file") as File;
    expect(uploaded.name).toBe("lincs-caffeic-acid-mcf7-a549.csv");
    expect(form.get("format")).toBe("csv");
  });

  it("JSON paste works as the secondary advanced path", async () => {
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    fireEvent.click(screen.getByRole("button", { name: /Advanced: paste JSON/i }));
    fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
      target: { value: '{"A1BG":0.1}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Load signature$/i }));
    const runBtn = await screen.findByRole("button", { name: /^Analyze signature$/i });
    await waitFor(() => expect(runBtn).not.toBeDisabled());
    fireEvent.click(runBtn);
    expect((await screen.findAllByRole("article")).length).toBe(2);
  });

  it("all-incompatible input has a dedicated state and cannot run", async () => {
    installFetchMock({
      "POST /api/signatures/parse": {
        body: {
          ...parseOk,
          ready: false,
          compatible_endpoint_ids: [],
          compatibility: parseOk.compatibility.map((item) => ({
            ...item,
            compatible: false,
            n_missing: 12,
            reason: "missing required genes",
          })),
        },
      },
    });
    renderApp("/analyze");
    await screen.findByRole("heading", { name: /Analyze a gene-expression signature/i });
    uploadCsv();
    expect(await screen.findByText(/No compatible endpoints/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Use this signature/i })).toBeDisabled();
  });
});
