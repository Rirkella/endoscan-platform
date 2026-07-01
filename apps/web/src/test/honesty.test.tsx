import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { PredictionResultView } from "../components/PredictionResultView";
import type { PredictionResult } from "../api/types";
import { installFetchMock, predictAR } from "./mockApi";
import { renderApp } from "./renderApp";

const FORBIDDEN = [
  /\bendocrine disruptor confirmed\b/i,
  /\bconfirmed (?:toxic|safe)\b/i,
  /\bis toxic\b/i,
  /\bis safe\b/i,
  /\bdiagnos(is|tic determination made)\b/i,
];

describe("honesty-first surfaces", () => {
  beforeEach(() => installFetchMock());

  it("the experimental banner is present on every route", async () => {
    renderApp("/");
    expect(screen.getByRole("note")).toHaveTextContent(
      /not a\s+regulatory, clinical, or diagnostic tool/i,
    );
    renderApp("/analyze");
    expect(screen.getAllByRole("note").length).toBeGreaterThan(0);
  });

  it("a positive-call result never overclaims (no toxic/safe/diagnostic language)", () => {
    const result = {
      ...(predictAR as unknown as PredictionResult),
      call: true,
      probability: 0.91,
    };
    render(<PredictionResultView result={result} target="Androgen Receptor" />);
    // Honest header + hedged score wording.
    expect(
      screen.getByText(/Transcriptomic signal consistent with Androgen Receptor activity/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/not a probability of real-world toxicity/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Not a regulatory, clinical, or diagnostic determination/i),
    ).toBeInTheDocument();
    // No overclaiming copy anywhere in the rendered result.
    const text = document.body.textContent ?? "";
    for (const pattern of FORBIDDEN) {
      expect(text).not.toMatch(pattern);
    }
  });
});
