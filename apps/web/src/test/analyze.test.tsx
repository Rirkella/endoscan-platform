// Analyze-first flow — the primary redesign. API mocked with fixtures captured from the real API.

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, predictAR } from "./mockApi";
import { renderApp } from "./renderApp";

const SIG_JSON = '{"A1BG":0.1}'; // shape-valid; the mocked API doesn't gene-validate

function loadSignature() {
  fireEvent.change(screen.getByLabelText(/Signature \(JSON/i), { target: { value: SIG_JSON } });
  fireEvent.click(screen.getByRole("button", { name: /Load signature/i }));
}

async function analyze() {
  loadSignature();
  // The Analyze button is disabled until /endpoints has loaded (fan-out needs the endpoint set).
  const btn = await screen.findByRole("button", { name: /Analyze across endpoints/i });
  await waitFor(() => expect(btn).not.toBeDisabled());
  fireEvent.click(btn);
}

// The endpoint's SCORE CARD (an <article>) — its target also appears in the comparison bar, so
// scope to the article rather than a bare getByText.
function cardFor(target: string): HTMLElement {
  const article = screen
    .getAllByText(target)
    .map((e) => e.closest("article"))
    .find((a): a is HTMLElement => a != null);
  if (!article) throw new Error(`no score card for ${target}`);
  return article;
}

describe("Analyze-first IA + score cards", () => {
  beforeEach(() => installFetchMock());

  it("Analyze is the default route (/)", async () => {
    renderApp("/");
    expect(await screen.findByRole("heading", { name: /Analyze a signature/i })).toBeInTheDocument();
    // Phase 2: the upload zone is now ENABLED (a functional file input, not the disabled placeholder).
    expect(screen.getByText(/Upload a signature file/i)).toBeInTheDocument();
    const fileIn = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileIn).not.toBeDisabled();
    // Explore is still a disabled "coming later" chip — no route, no fake content.
    const explore = screen.getByText(/Explore/i).closest("span")!;
    expect(explore).toHaveAttribute("aria-disabled", "true");
  });

  it("score cards render from the mocked API (ER + AR), not hardcoded", async () => {
    renderApp("/");
    await analyze();
    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(2);
    expect(screen.getAllByText("Estrogen Receptor").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Androgen Receptor").length).toBeGreaterThanOrEqual(1);
    // Honest wording — signal score / model call, never risk/toxic/safe.
    expect(screen.getAllByText(/Endpoint signal score/i).length).toBe(2);
    expect(screen.getAllByText(/Model call/i).length).toBe(2);
    expect(screen.getAllByText(/^(Active|Inactive)$/).length).toBe(2);
  });

  it("SYNTHETIC 6-endpoint result renders 6 cards + an honest 'coming later' viz (scalability)", async () => {
    const six = Array.from({ length: 6 }, (_, i) => ({
      endpoint_id: `E${i + 1}`,
      biological_target: `Target ${i + 1}`,
      status: "experimental",
      input_type: "transcriptomics",
      frozen: false,
    }));
    const results = six.map((e) => ({
      endpoint_id: e.endpoint_id,
      biological_target: e.biological_target,
      ok: true,
      result: { ...predictAR, endpoint_id: e.endpoint_id },
      error: null,
    }));
    installFetchMock({
      "GET /api/endpoints": { body: six }, // so the Analyze button enables
      "POST /api/analyze": { body: { results } },
    });
    const { container } = renderApp("/");
    await analyze();

    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(6); // one per API result — no fixed 2-or-3 layout
    // >2 endpoints: honest placeholder, NOT a fake radar/heatmap.
    expect(container.querySelector('[data-viz="coming-later"]')).not.toBeNull();
    expect(container.querySelector('[data-viz="bars"]')).toBeNull();
  });

  it("2-endpoint comparison bar renders", async () => {
    const { container } = renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    expect(container.querySelector('[data-viz="bars"]')).not.toBeNull();
    expect(screen.getByText(/Endpoint signal comparison/i)).toBeInTheDocument();
  });
});

describe("gene contribution cards — correct method label per endpoint", () => {
  beforeEach(() => installFetchMock());

  it("ER explain shows TreeSHAP", async () => {
    renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    const erCard = cardFor("Estrogen Receptor");
    fireEvent.click(within(erCard).getByRole("button", { name: /Explain this call/i }));
    expect(await within(erCard).findByText(/Method:/i)).toHaveTextContent("TreeSHAP");
    // empty annotation slot exists (no fabricated text)
    expect(erCard.querySelector("[data-annotation-slot]")).not.toBeNull();
  });

  it("AR explain shows the linear-coefficient label (never SHAP)", async () => {
    renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    const arCard = cardFor("Androgen Receptor");
    fireEvent.click(within(arCard).getByRole("button", { name: /Explain this call/i }));
    expect(
      await within(arCard).findByText(
        /Linear coefficient attribution \(coefficient × value — not a SHAP value\)/i,
      ),
    ).toBeInTheDocument();
  });

  it("explain 501 renders a clean, non-scary state (live API drives availability)", async () => {
    installFetchMock({
      "POST /api/explain": {
        status: 501,
        body: {
          error: "explain_unsupported_for_model",
          detail: "no attributor for model type 'Foo'",
          endpoint_id: "AR",
        },
      },
    });
    renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    const arCard = cardFor("Androgen Receptor");
    fireEvent.click(within(arCard).getByRole("button", { name: /Explain this call/i }));
    expect(
      await within(arCard).findByText(/aren.t available for this endpoint.s model type/i),
    ).toBeInTheDocument();
  });
});

describe("integrated limitations + honesty", () => {
  beforeEach(() => installFetchMock());

  it("limitations are present and readable with every result (status + CI reason + scope + disclaimer)", async () => {
    renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    const erCard = cardFor("Estrogen Receptor");
    // status header, a CI-demotion reason, the scope label, and the disclaimer are all in the DOM.
    expect(within(erCard).getAllByTitle(/Model status: experimental/i).length).toBeGreaterThan(0);
    expect(
      within(erCard).getByText(/auroc 95% CI lower bound 0.675 < 0.75 floor/i),
    ).toBeInTheDocument();
    expect(within(erCard).getByText(/Scope of claim/i)).toBeInTheDocument();
    expect(within(erCard).getByText(/NOT regulatory-grade validation/i)).toBeInTheDocument();
  });

  it("the experimental banner is present and no overclaiming copy appears", async () => {
    renderApp("/");
    await analyze();
    await screen.findAllByRole("article");
    expect(screen.getByRole("note")).toHaveTextContent(
      /not a\s+regulatory, clinical, or diagnostic tool/i,
    );
    const text = document.body.textContent ?? "";
    for (const bad of [/risk score/i, /\btoxic\b/i, /\bis safe\b/i, /confirmed EDC/i]) {
      expect(text).not.toMatch(bad);
    }
  });
});

describe("Model Library (restructured detail)", () => {
  beforeEach(() => installFetchMock());

  it("lists endpoints from the API at /library", async () => {
    renderApp("/library");
    expect(await screen.findByText("Estrogen Receptor")).toBeInTheDocument();
    expect(screen.getByText("Androgen Receptor")).toBeInTheDocument();
    expect(screen.getAllByTitle(/Model status: experimental/i).length).toBeGreaterThanOrEqual(2);
  });

  it("evidence page renders limitations + a single-variant list at /library/:id", async () => {
    renderApp("/library/AR");
    expect(
      await screen.findByText("auprc 95% CI lower bound 0.420 < 0.50 floor"),
    ).toBeInTheDocument();
    // "Scope of claim" appears in the LimitationsPanel AND in the model-card <pre> here.
    expect(screen.getAllByText(/Scope of claim/i).length).toBeGreaterThan(0);
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument(); // one variant today
  });
});
