// Reference data — "Explore measured toxicology responses". The visible layer speaks biology
// (reference signatures, nearest signatures, close/moderately close/far, endpoint-specific labels);
// the technical fields (k-th distance, percentile, UMAP params, metric, feature space) live one
// level deeper in "Technical details". Honesty invariants: real points only, approximate placement
// (no exact projection), no in-/out-of-domain verdict in the visible layer, no fabricated points,
// and active/inactive framed as endpoint-dataset labels (not universal safe/harmful).

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
  // JSON input lives in "Advanced technical input"; jsdom keeps <details> content queryable.
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
    target: { value: '{"A1BG":0.1}' },
  });
  fireEvent.click(screen.getByRole("button", { name: /^Load signature$/i }));
  await screen.findByTestId("similarity-readout");
}

describe("Reference data view", () => {
  beforeEach(() => installFetchMock());

  it("Reference-data nav points to the real /explore route", async () => {
    renderApp("/analyze");
    const link = await screen.findByRole("link", { name: /Reference data/i });
    expect(link).toHaveAttribute("href", "/explore");
  });

  it("renders the scatter of REAL reference signatures + plain, endpoint-labelled framing", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    expect(pointCircles().length).toBe(7); // one circle per real signature, none fabricated
    expect(screen.getByRole("heading", { name: /Explore measured toxicology responses/i })).toBeInTheDocument();
    // Correct entity naming + real summary counts.
    expect(screen.getAllByText(/reference signatures/i).length).toBeGreaterThan(0);
    // Endpoint-specific labels, not a bare "active/inactive".
    expect(screen.getByText(/Labelled active for this endpoint/i)).toBeInTheDocument();
    expect(
      screen.getByText(/not a general statement that a compound is safe or harmful/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/not a model boundary/i)).toBeInTheDocument();
  });

  it("keeps the unknown (unlabeled) point DISTINCT — never colored as a class", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    expect(document.querySelectorAll("circle[data-label='unlabeled']").length).toBe(1);
    expect(document.querySelectorAll("circle[data-label='active']").length).toBe(3);
    expect(document.querySelectorAll("circle[data-label='inactive']").length).toBe(3);
    // The unlabeled category is surfaced honestly in the legend (its own state, not a class).
    expect(screen.getByText(/No label in this dataset/i)).toBeInTheDocument();
  });

  it("placement uses biological wording: nearest reference signatures + a graded readout", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    // Graded plain statement (fixture percentile 0.42 -> "close").
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/close to/i);
    expect(screen.getByText(/Nearest reference signatures/i)).toBeInTheDocument();
    expect(screen.getByText(/does not prove the same effect/i)).toBeInTheDocument();
    // Approximate-position language on the placement.
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/Approximate position/i);
    // The scatter highlights the similar signatures + shows an approximate marker.
    await waitFor(() =>
      expect(document.querySelectorAll("circle[data-neighbor='true']").length).toBe(3),
    );
    expect(screen.getByTestId("explore-approx-marker")).toBeInTheDocument();
  });

  it("the VISIBLE layer carries no technical jargon; the technical fields live in Technical details", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    const visible = visibleText();
    for (const banned of ["978-d", "nearest neighbor", "k-th", "percentile", "in-domain", "out-of-domain"]) {
      expect(visible).not.toContain(banned);
    }

    const tech = (screen.getByTestId("explore-technical").textContent ?? "").toLowerCase();
    expect(tech).toContain("percentile");
    expect(tech).toContain("umap");
    expect(tech).toContain("random_state");
    expect(tech).toContain("euclidean");
    expect(tech).toContain("nearest reference signature");
    expect(tech).toContain("feature schema");
  });

  it("the close/far label maps transparently from the real percentile", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    const tech = screen.getByTestId("explore-technical").textContent ?? "";
    expect(tech).toMatch(/42\.0%/);
    expect(tech.toLowerCase()).toContain("≤50% → close");
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/close/i);
  });

  it("a not-computed context shows an honest empty state with ZERO fabricated points", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter"); // ER map present first

    fireEvent.change(screen.getByLabelText(/Endpoint/i), { target: { value: "AR" } });

    expect(await screen.findByText(/No reference map for AR yet/i)).toBeInTheDocument();
    expect(pointCircles().length).toBe(0); // no placeholder / fake points
  });
});
