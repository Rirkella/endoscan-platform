// Reference data — "Explore measured toxicology responses". The visible layer speaks biology
// (reference signatures, nearest signatures, close/moderately close/far, endpoint-specific labels);
// the technical fields (k-th distance, percentile, UMAP params, metric, feature space) live one
// level deeper in "Technical details". Honesty invariants: real points only, approximate placement
// (no exact projection), no in-/out-of-domain verdict in the visible layer, no fabricated points,
// and active/inactive framed as endpoint-dataset labels (not universal safe/harmful).

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import type { ExploreLocateResult, ExplorePoint } from "../api/types";
import { ExploreScatter } from "../components/ExploreScatter";
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

    // Placement and neighbour similarity are described separately.
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/Approximate position/i);
    expect(screen.getByText(/Most similar full gene-expression profiles/i)).toBeInTheDocument();
    expect(screen.getByText(/does not prove the same effect/i)).toBeInTheDocument();
    expect(screen.queryByText(/very similar response|rank 1 of 7/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/ER: Active/i).length).toBeGreaterThan(0);
    // The scatter highlights the similar signatures + shows an approximate marker.
    await waitFor(() =>
      expect(document.querySelectorAll("circle[data-neighbor='true']").length).toBe(5),
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

  it("keeps isolation percentile separate from empirical neighbour similarity", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    await placeSignature();

    const tech = screen.getByTestId("explore-technical").textContent ?? "";
    expect(tech).toMatch(/42\.0%/);
    expect(tech.toLowerCase()).toContain("separate from neighbour similarity ranks");
    expect(screen.getByTestId("similarity-readout")).not.toHaveTextContent(/close/i);
    expect(screen.getByTestId("similar-compounds")).toHaveTextContent(/Caffeic Acid/i);
    expect(screen.getByTestId("similar-compounds")).not.toHaveTextContent(/very similar response/i);
  });

  it("a not-computed context shows an honest empty state with ZERO fabricated points", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter"); // ER map present first

    fireEvent.change(screen.getByLabelText(/Endpoint/i), { target: { value: "AR" } });

    expect(await screen.findByText(/No reference map for AR yet/i)).toBeInTheDocument();
    expect(pointCircles().length).toBe(0); // no placeholder / fake points
  });
});

it("marks an existing reference at exact stored coordinates without approximate language", () => {
  const point: ExplorePoint = {
    compound_id: "EXACT",
    x: 1,
    y: 2,
    label: "active",
    preferred_name: "Exact compound",
    pubchem_cid: 1,
    source_dataset: "Measured source",
    experimental_contexts: [],
    full_signature_id: "exact-signature",
  };
  const exact = {
    ...point,
    distance: 0,
    similarity_category: "exact match",
    similarity_rank: 0,
    similarity_percentile: 0,
  };
  const locate: ExploreLocateResult = {
    context: "ER",
    placement: "exact_existing_reference",
    approx_xy: { x: point.x, y: point.y },
    exact_match: exact,
    neighbors: [],
    domain: {
      metric: "distance_to_kth_training_neighbor",
      k: 1,
      query_kth_distance: 1,
      training_reference_quantiles: {},
      percentile: 0,
    },
  };
  render(<ExploreScatter points={[point]} locate={locate} />);
  expect(screen.getByTestId("explore-exact-marker")).toHaveTextContent(
    /Exact existing reference record/i,
  );
  expect(screen.queryByTestId("explore-approx-marker")).not.toBeInTheDocument();
});
