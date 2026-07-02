// Explore / reference-landscape view. Product-language rule: the VISIBLE layer speaks biology
// (similar compounds, close/moderately close/far), the technical fields (k-th distance, percentile,
// UMAP params, metric, feature space) live one level deeper in a collapsible "Technical details".
// Honesty invariants are unchanged: real training points only, approximate placement (no exact
// projection), no in-/out-of-domain verdict in the visible layer, no fabricated points.

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

function pointCircles(): NodeListOf<Element> {
  return document.querySelectorAll("circle[data-compound]");
}

// The visible layer = everything EXCEPT the collapsible Technical details block.
function visibleText(): string {
  const body = (document.body.textContent ?? "").toLowerCase();
  const details = (screen.queryByTestId("explore-technical")?.textContent ?? "").toLowerCase();
  return details ? body.split(details).join(" ") : body;
}

async function placeSignature() {
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
    target: { value: '{"A1BG":0.1}' },
  });
  fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));
  await screen.findByTestId("similarity-readout");
}

describe("Explore reference-landscape view", () => {
  beforeEach(() => installFetchMock());

  it("Explore nav is enabled (not the old disabled 'coming later' chip)", async () => {
    renderApp("/");
    const link = await screen.findByRole("link", { name: /^Explore$/i });
    expect(link).toHaveAttribute("href", "/explore");
  });

  it("renders the scatter of REAL reference points + a plain, biological caption", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    expect(pointCircles().length).toBe(7); // one circle per real compound, none fabricated
    expect(await screen.findByText(/7 training compounds/i)).toBeInTheDocument();
    expect(screen.getByText(/3 active/i)).toBeInTheDocument();
    // Biological framing, not "UMAP".
    expect(screen.getByRole("heading", { name: /Reference landscape/i })).toBeInTheDocument();
    expect(screen.getByText(/not a model boundary, and not proof of anything/i)).toBeInTheDocument();
  });

  it("placement uses biological wording: similar compounds + a graded close/far readout", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    // The graded plain statement (fixture percentile 0.42 -> "close").
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/close to/i);
    expect(screen.getByText(/How similar is this signature to known data\?/i)).toBeInTheDocument();
    // Neighbour list is framed as comparisons, not a claim.
    expect(screen.getByText(/Compounds with similar expression patterns/i)).toBeInTheDocument();
    expect(screen.getByText(/do not prove the same effect/i)).toBeInTheDocument();
    // Approximate-position language on the placement (visible + as the marker).
    expect(
      screen.getAllByText(/Approximate position based on the most similar known signatures/i).length,
    ).toBeGreaterThan(0);
    // The scatter still highlights the similar compounds + shows an approximate marker.
    await waitFor(() =>
      expect(document.querySelectorAll("circle[data-neighbor='true']").length).toBe(3),
    );
    expect(screen.getByTestId("explore-approx-marker")).toBeInTheDocument();
  });

  it("the VISIBLE layer carries no technical jargon; the technical fields live in Technical details", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    // Visible layer (excluding the collapsible) must be free of technical jargon.
    const visible = visibleText();
    for (const banned of ["978-d", "nearest neighbor", "k-th", "percentile", "in-domain", "out-of-domain"]) {
      expect(visible).not.toContain(banned);
    }

    // The technical fields ARE present, one level deeper.
    const tech = (screen.getByTestId("explore-technical").textContent ?? "").toLowerCase();
    expect(tech).toContain("percentile");
    expect(tech).toContain("umap"); // projection method + params
    expect(tech).toContain("random_state");
    expect(tech).toContain("euclidean"); // distance metric
    expect(tech).toContain("nearest reference signature"); // the k-th distance field
    expect(tech).toContain("feature schema"); // feature space
  });

  it("the close/far label maps transparently from the real percentile", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    // Fixture percentile = 0.42 -> shown as 42.0% in the details, banded to "close" (<=50%).
    const tech = screen.getByTestId("explore-technical").textContent ?? "";
    expect(tech).toMatch(/42\.0%/);
    expect(tech.toLowerCase()).toContain("≤50% → close");
    // ...and the visible readout reflects exactly that band.
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/close/i);
  });

  it("a not-computed context shows an honest empty state with ZERO fabricated points", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter"); // ER map present first

    fireEvent.change(screen.getByLabelText(/Endpoint/i), { target: { value: "AR" } });

    expect(await screen.findByText(/Reference landscape not yet available/i)).toBeInTheDocument();
    expect(pointCircles().length).toBe(0); // no placeholder / fake points
  });
});
