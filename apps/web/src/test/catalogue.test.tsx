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
    expect(screen.getByText(/LINCS L1000 · GSE92742/i)).toBeInTheDocument();
    expect(screen.getByText(/dose\/time not available/i)).toBeInTheDocument();
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
    expect(await screen.findByText(/No measured public signature found/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Use selected/i })).not.toBeInTheDocument();
  });
});
