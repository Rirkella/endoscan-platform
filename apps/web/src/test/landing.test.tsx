// Landing page (production, "/"), ported from prototype-v2. Locks the honest framing (experimental
// / research-use only) and that every call-to-action points at a REAL route — no fake pages.

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

const REAL_ROUTES = new Set(["/analyze", "/library", "/explore"]);

describe("Landing page", () => {
  it('is the default route "/" with the prototype hero and research-use framing', async () => {
    installFetchMock();
    renderApp("/");
    expect(
      await screen.findByRole("heading", { name: /See biological signals earlier/i }),
    ).toBeInTheDocument();
    // Honest positioning: experimental research platform, research-use only (never a safety claim).
    expect(screen.getAllByText(/Experimental research platform/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Experimental research use only/i)).toBeInTheDocument();
    expect(
      screen.getByText(/EndoScan does not replace experimental validation/i),
    ).toBeInTheDocument();
  });

  it("every CTA navigates ONLY to existing real routes", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /See biological signals earlier/i });
    // Primary workspace CTAs.
    for (const link of screen.getAllByRole("link", { name: /Open the workspace/i })) {
      expect(link).toHaveAttribute("href", "/analyze");
    }
    expect(screen.getByRole("link", { name: /Review model evidence/i })).toHaveAttribute(
      "href",
      "/library",
    );
    expect(screen.getByRole("link", { name: /Explore reference data/i })).toHaveAttribute(
      "href",
      "/explore",
    );
    expect(screen.getByRole("link", { name: /Enter the EndoScan workspace/i })).toHaveAttribute(
      "href",
      "/analyze",
    );
    // No CTA points anywhere outside the real route set.
    for (const link of screen.getAllByRole("link")) {
      const href = link.getAttribute("href") ?? "";
      if (href.startsWith("/")) expect(REAL_ROUTES.has(href) || href === "/").toBe(true);
    }
  });

  it("shows the current-scope proof strip (real, honest anchors)", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /See biological signals earlier/i });
    expect(screen.getByText(/landmark genes checked before analysis/i)).toBeInTheDocument();
    expect(screen.getByText(/registered endocrine endpoint models/i)).toBeInTheDocument();
    expect(screen.getByText(/evidence layers: genes, pathways, references/i)).toBeInTheDocument();
    expect(screen.getAllByText("978").length).toBeGreaterThan(0);
  });
});
