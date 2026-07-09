// Biological pathways panel. The visible layer is biology (pathway names, the genes from this
// result, an evidence label) with the required honesty captions; the statistics live only in the
// collapsible Technical details. Empty / too-few / unavailable are honest plain-language states.

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { PathwaysPanel } from "../components/PathwaysPanel";
import { installFetchMock, pathwaysOk } from "./mockApi";

const SIG = { DDR1: 0.1 };

// Visible layer = everything EXCEPT the collapsible Technical details blocks (per-card + method).
function visibleText(): string {
  const body = (document.body.textContent ?? "").toLowerCase();
  let out = body;
  for (const el of document.querySelectorAll(
    '[data-testid="pathway-technical"], [data-testid="pathways-method-block"]',
  )) {
    const t = (el.textContent ?? "").toLowerCase();
    if (t) out = out.split(t).join(" ");
  }
  return out;
}

const BANNED_TECHNICAL = [
  "ora",
  "fisher",
  "fdr",
  "p-value",
  "adjusted p",
  "enrichment",
  "over-representation",
  "universe",
  "hypergeometric",
];
const BANNED_CAUSAL = ["activates", "causes", "perturbed", "affected gene"];

describe("Biological pathways panel", () => {
  beforeEach(() => installFetchMock());

  it("renders the two required honesty captions (framing + landmark panel)", async () => {
    installFetchMock({ "POST /api/interpret/pathways": { body: pathwaysOk } });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await screen.findByText(/Signaling by Nuclear Receptors/i);
    // Framing: model-contributing, clues not proof.
    expect(screen.getByTestId("pathways-framing")).toHaveTextContent(
      /Genes that influenced this result .* clues for further investigation, not proof/i,
    );
    // Landmark-panel caption (prevents over-reading a broad "High" pathway).
    expect(screen.getByTestId("pathways-landmark-caption")).toHaveTextContent(
      /978 landmark genes .* not the full transcriptome/i,
    );
  });

  it("shows biology in the visible layer: name, the genes from THIS result, evidence label, source", async () => {
    installFetchMock({ "POST /api/interpret/pathways": { body: pathwaysOk } });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await screen.findByText(/Signaling by Nuclear Receptors/i);
    expect(screen.getByText("ESR1")).toBeInTheDocument(); // the actual overlap gene
    expect(screen.getAllByText(/Genes from this result in this pathway/i).length).toBe(2);
    expect(screen.getAllByText(/evidence/i).length).toBeGreaterThan(0); // High/Low badges
    expect(screen.getAllByText(/Source: Reactome/i).length).toBeGreaterThan(0);
  });

  it("keeps statistical jargon OUT of the visible layer; Technical details HAS p/q/test/universe/version", async () => {
    installFetchMock({ "POST /api/interpret/pathways": { body: pathwaysOk } });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await screen.findByText(/Signaling by Nuclear Receptors/i);

    const visible = visibleText();
    for (const banned of BANNED_TECHNICAL) {
      expect(visible).not.toContain(banned);
    }
    // Nothing causal anywhere (including Technical details).
    const all = (document.body.textContent ?? "").toLowerCase();
    for (const banned of BANNED_CAUSAL) {
      expect(all).not.toContain(banned);
    }
    // The statistics ARE present, one level deeper.
    const method = (screen.getByTestId("pathways-method-block").textContent ?? "").toLowerCase();
    expect(method).toContain("fisher");
    expect(method).toContain("benjamini-hochberg");
    expect(method).toContain("universe");
    expect(method).toContain("fixture-not-real"); // reactome version
    expect(method).toContain("cc0"); // reactome license
    const perCard = (screen.getAllByTestId("pathway-technical")[0].textContent ?? "").toLowerCase();
    expect(perCard).toContain("p-value");
    expect(perCard).toContain("adjusted p");
  });

  it("unavailable Reactome -> honest empty state, no fabricated cards", async () => {
    installFetchMock(); // default route is the unavailable fixture
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await screen.findByText(/Pathway information isn.t available for this result yet/i);
    expect(screen.queryByText(/Source: Reactome/i)).not.toBeInTheDocument();
  });

  it("too-few contributing genes -> honest refusal", async () => {
    installFetchMock({
      "POST /api/interpret/pathways": {
        body: { endpoint_id: "ER", method: "tree_shap", status: "too_few_genes", reason: "x", pathways: [], method_block: null },
      },
    });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await screen.findByText(/Too few contributing genes for reliable pathway analysis/i);
  });

  it("ran but nothing cleared the threshold -> 'No pathways met the evidence threshold'", async () => {
    installFetchMock({
      "POST /api/interpret/pathways": {
        body: { endpoint_id: "ER", method: "tree_shap", status: "ok", reason: null, pathways: [], method_block: null },
      },
    });
    render(<PathwaysPanel endpointId="ER" signature={SIG} />);
    await waitFor(() =>
      expect(screen.getByText(/No pathways met the evidence threshold for this result/i)).toBeInTheDocument(),
    );
  });
});
