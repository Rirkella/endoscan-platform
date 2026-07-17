import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { catalogueSearch, installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

async function openCatalogue() {
  renderApp("/analyze");
  fireEvent.click(await screen.findByRole("tab", { name: /Public catalogue/i }));
}

describe("measured public signature catalogue", () => {
  beforeEach(() => installFetchMock());

  it("searches a verified identity and shows only its measured record metadata", async () => {
    await openCatalogue();
    fireEvent.change(screen.getByRole("textbox", { name: /Search molecule/i }), {
      target: { value: "caffeic acid" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Search$/i }));

    expect(await screen.findByText("Caffeic Acid")).toBeInTheDocument();
    expect(screen.getByText(/PubChem CID 689043/i)).toBeInTheDocument();
    expect(screen.getByText(/LINCS L1000.*Level 5 COMPZ.MODZ/i)).toBeInTheDocument();
    expect(screen.getByText(/Missing dose and time metadata remain explicitly unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/never the name or structure alone/i)).toBeInTheDocument();
  });

  it("loads the selected measured signature through the common parse path and analyzes it", async () => {
    await openCatalogue();
    fireEvent.change(screen.getByRole("textbox", { name: /Search molecule/i }), {
      target: { value: "caffeic acid" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Search$/i }));
    const useButton = await screen.findByRole("button", {
      name: /Use selected measured signature/i,
    });
    await waitFor(() => expect(useButton).not.toBeDisabled());
    fireEvent.click(useButton);

    const runButton = await screen.findByRole("button", {
      name: /^Analyze signature$/i,
    });
    await waitFor(() => expect(runButton).not.toBeDisabled());
    expect(screen.getByText(/^PUBLIC$/i)).toBeInTheDocument();

    const calls = vi.mocked(fetch).mock.calls;
    expect(calls.some(([input]) => String(input).includes("/catalogue/v1/signatures/"))).toBe(true);
    const parseCall = calls.find(([input]) => String(input).includes("/signatures/parse"));
    expect(parseCall?.[1]?.body).toBeInstanceOf(FormData);

    fireEvent.click(runButton);
    expect((await screen.findAllByRole("article")).length).toBe(2);
  });

  it("renders an honest empty state without substituting an inferred result", async () => {
    installFetchMock({
      "GET /api/catalogue/v1/compounds": {
        body: { ...catalogueSearch, query: "unknown", results: [] },
      },
    });
    await openCatalogue();
    fireEvent.change(screen.getByRole("textbox", { name: /Search molecule/i }), {
      target: { value: "unknown" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Search$/i }));
    expect(await screen.findByText(/No known compound identity or compatible reference record matched/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Use selected/i })).not.toBeInTheDocument();
  });

  it("finds exact Bisphenol A identity while withholding Analyze without a compatible signature", async () => {
    installFetchMock({
      "GET /api/catalogue/v1/compounds": {
        body: {
          ...catalogueSearch,
          query: "IISBACLAFKSPIT-UHFFFAOYSA-N",
          results: [{
            compound_id: "IISBACLAFKSPIT-UHFFFAOYSA-N",
            preferred_name: "Bisphenol A",
            aliases: ["BPA", "80-05-7"],
            pubchem_cid: 6623,
            iupac_name: "4-[2-(4-hydroxyphenyl)propan-2-yl]phenol",
            canonical_smiles: "CC(C)(C1=CC=C(C=C1)O)C2=CC=C(C=C2)O",
            isomeric_smiles: null,
            signatures: [],
            availability_status: "Identity known — no compatible measured signature",
            availability_reason: "Present in the CERAPP endpoint source, but absent from the committed LINCS-derived support overlap.",
            reference_contexts: [],
            source_compound_ids: ["80-05-7"],
            lincs_perturbagen_ids: [],
          }],
        },
      },
    });
    await openCatalogue();
    fireEvent.change(screen.getByRole("textbox", { name: /Search molecule/i }), {
      target: { value: "IISBACLAFKSPIT-UHFFFAOYSA-N" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Search$/i }));
    expect(await screen.findByText("Bisphenol A")).toBeInTheDocument();
    expect(screen.getByText(/Identity known — no compatible measured signature/i)).toBeInTheDocument();
    expect(screen.getByText(/present in the CERAPP endpoint source.*absent from the committed LINCS-derived support overlap/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Use selected measured signature/i })).toBeDisabled();
  });
});
