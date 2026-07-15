import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { catalogueSearch, exploreUmap, installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

describe("interactive real reference explorer", () => {
  beforeEach(() => installFetchMock());

  it("zooms, resets, filters by available endpoint labels, and selects by keyboard", async () => {
    renderApp("/explore");
    const scatter = await screen.findByTestId("explore-scatter");
    expect(scatter).toHaveAttribute("viewBox", "0 0 520 360");

    fireEvent.click(screen.getByRole("button", { name: /Zoom in/i }));
    await waitFor(() => expect(scatter).not.toHaveAttribute("viewBox", "0 0 520 360"));
    fireEvent.click(screen.getByRole("button", { name: /^Reset$/i }));
    expect(scatter).toHaveAttribute("viewBox", "0 0 520 360");

    fireEvent.change(screen.getByLabelText(/Dataset class/i), {
      target: { value: "active" },
    });
    expect(document.querySelector("circle[data-label='inactive']")).toHaveAttribute(
      "display",
      "none",
    );

    const activePoint = document.querySelector(
      "circle[data-label='active']",
    ) as SVGCircleElement;
    fireEvent.keyDown(activePoint, { key: "Enter" });
    expect(
      await screen.findByRole("dialog", { name: /Reference signature details/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/one measured compound-level/i)).toBeInTheDocument();
    expect(screen.getByText(/Proximity does not prove a shared mechanism/i)).toBeInTheDocument();
  });

  it("searches an available name, highlights its map point, and opens normal Analyze", async () => {
    const compoundId = catalogueSearch.results[0].compound_id;
    const namedMap = {
      ...exploreUmap,
      points: exploreUmap.points.map((point, index) =>
        index === 0 ? { ...point, compound_id: compoundId } : point,
      ),
    };
    installFetchMock({ "GET /api/explore/ER/umap": { body: namedMap } });
    const view = renderApp("/explore");
    await screen.findByTestId("explore-scatter");

    fireEvent.change(screen.getByRole("textbox", { name: /Search reference signatures/i }), {
      target: { value: "Caffeic Acid" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Find$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Caffeic Acid/i }));

    await screen.findByRole("heading", { name: "Caffeic Acid" });
    expect(document.querySelector(`circle[data-compound='${compoundId}']`)).toHaveAttribute(
      "data-selected",
      "true",
    );
    expect(
      await screen.findByRole("button", { name: /Analyze this measured signature/i }),
    ).toBeInTheDocument();
    // The action targets this shareable Analyze URL. Render it directly because jsdom's
    // AbortSignal implementation is incompatible with React Router's navigation Request.
    view.unmount();
    const signatureId = catalogueSearch.results[0].signatures[0].signature_id;
    renderApp(`/analyze?catalogue_signature=${encodeURIComponent(signatureId)}`);
    const runButton = await screen.findByRole("button", {
      name: /Run \d+ compatible endpoint model/i,
    });
    expect(runButton).not.toBeDisabled();
    expect(screen.getByText(/^PUBLIC$/i)).toBeInTheDocument();
  });

  it("keeps an honest unavailable action for points absent from the small catalogue", async () => {
    renderApp("/explore");
    await screen.findByTestId("explore-scatter");
    fireEvent.click(document.querySelector("circle[data-compound='CID_00']") as SVGCircleElement);
    expect(await screen.findByText(/no full measured vector/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Analyze this measured signature/i }),
    ).not.toBeInTheDocument();
  });
});
