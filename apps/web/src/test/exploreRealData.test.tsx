// Explore against the REAL committed ER artifacts (models/ER/explore/*). Proves the Reference
// landscape renders real data (not a fixture) AND that the product-language rule still holds on
// real data: biological language in the visible layer, technical fields only in Technical details.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

// Read the real committed artifacts (repo root is two levels up from apps/web at test time).
const REPO = resolve(process.cwd(), "../..");
const realUmap = JSON.parse(readFileSync(resolve(REPO, "models/ER/explore/umap.json"), "utf-8"));
const realManifest = JSON.parse(
  readFileSync(resolve(REPO, "models/ER/explore/manifest.json"), "utf-8"),
);

// The API GET /explore/ER/umap shape (points + counts + the manifest SUMMARY the API builds).
const umapResponse = {
  context: "ER",
  points: realUmap.points,
  counts: realUmap.counts,
  manifest: {
    target: realManifest.target,
    n_compounds: realManifest.n_compounds,
    umap: realManifest.umap,
    domain_metric_k: realManifest.domain_metric.k,
    label_status: realManifest.labels.status,
    source_sha256: realManifest.source.sha256,
    source_key: realManifest.source.key,
    point_definition: realManifest.point_definition,
    aggregation: realManifest.aggregation,
    built_at: realManifest.built_at,
  },
};

// A realistic POST /explore/locate response built from the real map (first 5 points as the
// nearest neighbours, real domain quantiles, a real percentile -> "close" band).
const locateResponse = {
  context: "ER",
  placement: "approximate_nearest_neighbor",
  approx_xy: { x: realUmap.points[0].x, y: realUmap.points[0].y },
  neighbors: realUmap.points.slice(0, 5).map((p: { compound_id: string; x: number; y: number; label: string | null }, i: number) => ({
    compound_id: p.compound_id,
    distance: 0.1 * i,
    x: p.x,
    y: p.y,
    label: p.label,
    preferred_name: null,
    pubchem_cid: null,
    source_dataset: "LINCS L1000",
    experimental_contexts: [],
    full_signature_id: null,
    similarity_category: i === 0 ? "very similar response" : i < 3 ? "similar response" : "moderately similar response",
    similarity_rank: i + 1,
    similarity_percentile: (i + 1) / realUmap.counts.n_total,
  })),
  exact_match: null,
  domain: {
    metric: "distance_to_kth_training_neighbor",
    k: realManifest.domain_metric.k,
    query_kth_distance: realManifest.domain_metric.quantiles.median,
    training_reference_quantiles: realManifest.domain_metric.quantiles,
    percentile: 0.345,
  },
};

function visibleText(): string {
  const body = (document.body.textContent ?? "").toLowerCase();
  const details = (screen.queryByTestId("explore-technical")?.textContent ?? "").toLowerCase();
  return details ? body.split(details).join(" ") : body;
}

describe("Explore against REAL committed ER artifacts", () => {
  beforeEach(() =>
    installFetchMock({
      "GET /api/explore/ER/umap": { body: umapResponse },
      "POST /api/explore/locate": { body: locateResponse },
    }),
  );

  it("renders the real ER reference landscape (963 real signatures, real counts)", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    // Every real signature is a point — no fabrication, no truncation.
    expect(document.querySelectorAll("circle[data-compound]").length).toBe(realUmap.counts.n_total);
    // Real summary count + correct entity naming.
    expect(await screen.findByText(String(realUmap.counts.n_total))).toBeInTheDocument();
    expect(screen.getAllByText(/reference signatures/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Labelled active for this endpoint/i)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Explore measured toxicology responses/i }),
    ).toBeInTheDocument();
  });

  it("keeps biological language + hides technical jargon on REAL data", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), {
      target: { value: '{"A1BG":0.1}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));
    await screen.findByTestId("similarity-readout");

    // Visible layer: biological, and free of the banned technical strings.
    expect(screen.getByTestId("similarity-readout")).toHaveTextContent(/Approximate position/i);
    expect(screen.getByText(/Most similar full gene-expression profiles/i)).toBeInTheDocument();
    expect(screen.queryByText(/very similar response/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("neighbor-marker-1")).toBeInTheDocument();
    expect(screen.getByText(/2D map is a visual approximation/i)).toBeInTheDocument();
    const visible = visibleText();
    for (const banned of ["978-d", "nearest neighbor", "k-th", "percentile", "in-domain", "out-of-domain"]) {
      expect(visible).not.toContain(banned);
    }
    // Technical details carry the REAL provenance (seed 42, euclidean, the real UMAP version).
    const tech = (screen.getByTestId("explore-technical").textContent ?? "").toLowerCase();
    expect(tech).toContain("random_state=42");
    expect(tech).toContain("euclidean");
    expect(tech).toContain(String(realManifest.umap.umap_version).toLowerCase());
    // No in_domain verdict anywhere in the placement payload rendering.
    await waitFor(() => expect(screen.getByTestId("explore-approx-marker")).toBeInTheDocument());
  }, 15000);
});
