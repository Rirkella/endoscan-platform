// Explore / UMAP data-space view. The map is real training points served by the API; a submitted
// signature is placed by NEAREST NEIGHBOURS (approximate, never an exact projection); there is no
// in-/out-of-domain verdict; a not-computed context shows an honest empty state (zero fake points).

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

function pointCircles(): NodeListOf<Element> {
  return document.querySelectorAll("circle[data-compound]");
}

describe("Explore data-space view", () => {
  beforeEach(() => installFetchMock());

  it("Explore nav is enabled (not the old disabled 'coming later' chip)", async () => {
    renderApp("/");
    const link = await screen.findByRole("link", { name: /^Explore$/i });
    expect(link).toHaveAttribute("href", "/explore");
  });

  it("renders the scatter of REAL training points + a dataset-count caption", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    // 7 points from the fixture map — one circle per real compound, none fabricated.
    expect(pointCircles().length).toBe(7);
    expect(await screen.findByText(/7 training compounds/i)).toBeInTheDocument();
    expect(screen.getByText(/3 active/i)).toBeInTheDocument();
    // Honesty framing is always present (header + caption both say it).
    expect(screen.getAllByText(/not a model boundary and not proof/i).length).toBeGreaterThan(0);
  });

  it("placing a signature highlights nearest neighbours + shows approximate-placement copy", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");

    // Submit a signature via the JSON paste path (the API validates + returns neighbours).
    fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
      target: { value: '{"A1BG":0.1}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));

    // The side panel names the nearest known compound + the DEFINED domain metric.
    // (The phrase also appears as the approx-marker's SVG <title>, so match all.)
    expect((await screen.findAllByText(/Approximate placement \(nearest neighbors\)/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Nearest known compound/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/nearest training compound/i).length).toBeGreaterThan(0);
    // Neighbours are highlighted on the scatter (3 from the fixture) + an approx marker appears.
    await waitFor(() => expect(document.querySelectorAll("circle[data-neighbor='true']").length).toBe(3));
    expect(screen.getByTestId("explore-approx-marker")).toBeInTheDocument();
  });

  it("asserts NO in-/out-of-domain verdict anywhere in the DOM", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
      target: { value: '{"A1BG":0.1}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));
    await screen.findAllByText(/Approximate placement \(nearest neighbors\)/i);

    const text = (document.body.textContent ?? "").toLowerCase();
    expect(text).not.toContain("in-domain");
    expect(text).not.toContain("out-of-domain");
    expect(text).not.toContain("in domain");
    expect(text).not.toContain("out of domain");
  });

  it("a not-computed context shows an honest empty state with ZERO fabricated points", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter"); // ER map present first

    // Switch to AR, whose map returns 404 (not yet computed) in the mock.
    fireEvent.change(screen.getByLabelText(/Endpoint/i), { target: { value: "AR" } });

    expect(await screen.findByText(/Data-space map not yet computed/i)).toBeInTheDocument();
    expect(pointCircles().length).toBe(0); // no placeholder / fake points
  });
});
