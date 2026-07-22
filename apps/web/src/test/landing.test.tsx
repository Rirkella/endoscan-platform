// Landing page (production, "/"). Locks the broad-toxicology positioning, the honest research-use
// framing, and that every call-to-action points at a REAL route — no invented pages.

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

const REAL_ROUTES = new Set(["/analyze", "/library", "/explore"]);

describe("Landing page", () => {
  it('is the default route "/" with broad-toxicology positioning and research-use framing', async () => {
    installFetchMock();
    renderApp("/");
    expect(
      await screen.findByRole("heading", {
        name: /Explore measured cellular responses — and the biology behind them/i,
      }),
    ).toBeInTheDocument();
    // Broad platform positioning (not endocrine-only).
    expect(screen.getByText(/Mechanistic research, grounded in transcriptomics/i)).toBeInTheDocument();
    // Honest research-use framing.
    expect(screen.getAllByText(/Research use only · Experimental models/i).length).toBeGreaterThan(0);
    // ER/AR shown as the current available scope, not the whole platform.
    expect(screen.getByText(/Available today/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Experimental ER and AR models/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^Planned$/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Implemented foundation/i)).toBeInTheDocument();
    expect(screen.getByText(/Live and scientific validation remain pending/i)).toBeInTheDocument();
  });

  it("every CTA navigates ONLY to existing real routes", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /Explore measured cellular responses/i });
    for (const link of screen.getAllByRole("link", { name: /Start an analysis/i })) {
      expect(link).toHaveAttribute("href", "/analyze");
    }
    for (const link of screen.getAllByRole("link", { name: /Explore public data/i })) {
      expect(link).toHaveAttribute("href", "/explore");
    }
    expect(screen.getByRole("link", { name: /View model library/i })).toHaveAttribute(
      "href",
      "/library",
    );
    // No CTA points anywhere outside the real route set.
    for (const link of screen.getAllByRole("link")) {
      const href = link.getAttribute("href") ?? "";
      if (href.startsWith("/")) expect(REAL_ROUTES.has(href) || href === "/").toBe(true);
    }
  });

  it("keeps the 'more than a score' evidence framing (the strong result, moved up)", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /Explore measured cellular responses/i });
    expect(screen.getByText(/See the evidence behind every result/i)).toBeInTheDocument();
    expect(screen.getByText(/Illustrative example/i)).toBeInTheDocument();
    expect(
      screen.getByText(/EndoScan does not replace experimental validation/i),
    ).toBeInTheDocument();
  });
});
