// Landing page (production, "/"). Locks the honest framing + that CTAs go to REAL screens only.

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

describe("Landing page", () => {
  it('is the default route "/" and states research-use / experimental framing', async () => {
    installFetchMock();
    renderApp("/");
    expect(
      await screen.findByRole("heading", {
        name: /A platform for understanding molecular toxicity/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Research use only\. Experimental models\./i)).toBeInTheDocument();
  });

  it("CTAs navigate ONLY to existing real screens (/analyze, /explore)", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /molecular toxicity/i });
    // Every primary CTA points at a real route — no fake pages.
    for (const link of screen.getAllByRole("link", { name: /Analyze your data/i })) {
      expect(link).toHaveAttribute("href", "/analyze");
    }
    expect(screen.getByRole("link", { name: /Explore the platform/i })).toHaveAttribute(
      "href",
      "/explore",
    );
    expect(screen.getByRole("link", { name: /Explore EndoScan/i })).toHaveAttribute(
      "href",
      "/explore",
    );
  });

  it("separates current scope from future, and marks unsupported features as Planned", async () => {
    installFetchMock();
    renderApp("/");
    await screen.findByRole("heading", { name: /molecular toxicity/i });
    // The honest anchor + future separation are both present and visible.
    expect(screen.getByText(/Current scope/i)).toBeInTheDocument();
    expect(screen.getByText(/early endocrine endpoint models, including ER and AR/i)).toBeInTheDocument();
    expect(screen.getByText(/Future direction/i)).toBeInTheDocument();
    // Report export + broader endpoints are not shown as already-working ("Planned" markers exist).
    expect(screen.getAllByText(/^Planned$/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/^Available now$/i)).toBeInTheDocument();
  });
});
