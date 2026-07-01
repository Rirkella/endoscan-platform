import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { endpointAR, installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

describe("endpoints list + detail (data-driven from the API)", () => {
  beforeEach(() => installFetchMock());

  it("renders ER and AR from /endpoints with Experimental badges (not hardcoded)", async () => {
    renderApp("/endpoints");
    expect(await screen.findByText("Estrogen Receptor")).toBeInTheDocument();
    expect(screen.getByText("Androgen Receptor")).toBeInTheDocument();
    // Both endpoints are experimental → two Experimental badges, no green "validated".
    const badges = await screen.findAllByTitle(/Model status: experimental/i);
    expect(badges.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/Validated/i)).not.toBeInTheDocument();
  });

  it("detail renders limitations (CI reasons, scope, disclaimer) + a single-variant list", async () => {
    renderApp("/endpoints/AR");
    // CI-demotion reasons rendered verbatim from the API limitations block.
    expect(
      await screen.findByText("auroc 95% CI lower bound 0.678 < 0.75 floor"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("brier score 95% CI upper bound 0.243 > 0.20 ceiling"),
    ).toBeInTheDocument();
    // Scope verbatim (binding excluded / VCaP) and the disclaimer.
    expect(screen.getAllByText(/binding EXCLUDED/i).length).toBeGreaterThan(0);
    // The disclaimer phrase also appears in the model-card <pre>, so match all occurrences.
    expect(screen.getAllByText(/NOT regulatory-grade validation/i).length).toBeGreaterThan(0);
    // One variant today → no variant selector tablist.
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    // Metrics shown with a CI column (the phrase also appears in reasons/model card).
    expect(screen.getAllByText(/95% CI/i).length).toBeGreaterThan(0);
  });

  it("forward-compat: a SYNTHETIC 2-variant detail renders without breaking", async () => {
    const twoVariant = {
      ...endpointAR,
      variants: [
        endpointAR.variants[0],
        { ...endpointAR.variants[0], variant_id: "AR@ORGANOID" },
      ],
    };
    installFetchMock({ "GET /api/endpoints/AR": { body: twoVariant } });
    renderApp("/endpoints/AR");
    const tablist = await screen.findByRole("tablist");
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["AR", "AR@ORGANOID"]);
    // The active variant still renders its limitations.
    expect(
      screen.getByText("auroc 95% CI lower bound 0.678 < 0.75 floor"),
    ).toBeInTheDocument();
  });

  it("unknown endpoint renders a clean 404 notice", async () => {
    renderApp("/endpoints/NOPE");
    expect(await screen.findByText(/isn.t registered/i)).toBeInTheDocument();
  });
});
