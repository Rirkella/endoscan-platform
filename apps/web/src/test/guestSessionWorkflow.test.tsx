import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  AnalyzeResponse,
  EndpointSummary,
  LiteratureResponse,
  ParseResult,
} from "../api/types";
import type { EndpointSignal } from "../hooks/useAnalyze";
import {
  guestAnalysisRepository,
  type GuestAnalysisRecord,
  type PreparedAnalysisInput,
} from "../session/guestAnalysis";
import analyzeFixture from "./fixtures/analyze.json";
import endpointsFixture from "./fixtures/endpoints.json";
import explainERFixture from "./fixtures/explain_ER.json";
import parseFixture from "./fixtures/parse_ok.json";
import pathwaysFixture from "./fixtures/pathways_unavailable.json";
import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

function prepared(title = "Caffeic acid analysis"): PreparedAnalysisInput {
  const parse = parseFixture as ParseResult;
  return {
    title,
    subtitle: "Measured transcriptomic signature",
    kind: "paste",
    signature: parse.signature ?? { A1BG: 0 },
    parse,
    allowExtra: false,
    inputValueType: parse.input_value_type,
  };
}

function signals(): EndpointSignal[] {
  const response = analyzeFixture as AnalyzeResponse;
  const endpoints = endpointsFixture as EndpointSummary[];
  return response.results.map((item) => ({
    endpoint_id: item.endpoint_id,
    biological_target: item.biological_target,
    model_version: item.model_version,
    source_refs: item.source_refs,
    explanation: endpoints.find((endpoint) => endpoint.endpoint_id === item.endpoint_id)!.explanation,
    result: item.result,
    error: item.error,
  }));
}

const unavailableLiterature: LiteratureResponse = {
  endpoint_id: "ER",
  endpoint_name: "Estrogen receptor",
  status: "unavailable",
  reason: "PubMed integration is not configured.",
  articles: [],
  queries: [],
  provenance: {
    provider: "NCBI PubMed E-utilities",
    database: "pubmed",
    eutils_base_url: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    retrieved_at: "2026-07-15T00:00:00Z",
    tool: "endoscan",
    email_configured: false,
    api_key_used: false,
    rate_limit_per_second: 3,
    cache_hit: false,
  },
};

async function completedRecord(title = "Caffeic acid analysis"): Promise<GuestAnalysisRecord> {
  const endpoints = endpointsFixture as EndpointSummary[];
  const response = analyzeFixture as AnalyzeResponse;
  const created = await guestAnalysisRepository.create(prepared(title), endpoints);
  return guestAnalysisRepository.update(created.id, {
    status: "completed",
    analysis_result: { signals: signals(), summary: response.summary, run_error: null },
    selected_endpoint: "ER",
    last_viewed_tab: "endpoint",
    endpoint_explanations: { ER: explainERFixture as GuestAnalysisRecord["endpoint_explanations"][string] },
    endpoint_pathways: { ER: pathwaysFixture as GuestAnalysisRecord["endpoint_pathways"][string] },
    supporting_literature: { ER: unavailableLiterature },
  });
}

function requestKeys(): string[] {
  return vi.mocked(fetch).mock.calls.map(([input, init]) => {
    const url = new URL(typeof input === "string" ? input : input.toString(), "http://localhost");
    return `${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`;
  });
}

describe("saved analysis routing", () => {
  it("restores the exact endpoint tab from cache without rerunning model or evidence APIs", async () => {
    installFetchMock();
    const record = await completedRecord();
    const route = `/analyze/${record.id}?tab=endpoint&endpoint=ER`;

    const first = renderApp(route);
    expect(await screen.findByRole("heading", { name: "Estrogen Receptor evidence" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Supporting literature" })).toBeInTheDocument();
    first.unmount();

    renderApp(route);
    expect(await screen.findByRole("heading", { name: "Estrogen Receptor evidence" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Supporting literature" })).toBeInTheDocument());

    const calls = requestKeys();
    expect(calls).not.toContain("POST /api/analyze");
    expect(calls).not.toContain("POST /api/explain");
    expect(calls).not.toContain("POST /api/interpret/pathways");
    expect(calls).not.toContain("POST /api/interpret/literature");
    expect(calls).not.toContain("POST /api/explore/locate");

    fireEvent.click(screen.getByRole("button", { name: "Refresh literature" }));
    await waitFor(() => expect(requestKeys().filter((item) => item === "POST /api/interpret/literature")).toHaveLength(1));
    expect(requestKeys()).not.toContain("POST /api/analyze");
    expect(requestKeys()).not.toContain("POST /api/explain");
    expect(requestKeys()).not.toContain("POST /api/interpret/pathways");
    expect(requestKeys()).not.toContain("POST /api/explore/locate");
  });

  it("shows a clear current-session state for an unknown analysis id", async () => {
    installFetchMock();
    renderApp("/analyze/not-in-this-session");

    expect(await screen.findByRole("heading", {
      name: "This analysis is not available in the current browser session.",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to Projects" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start a new analysis" })).toBeInTheDocument();
  });
});

describe("Projects guest-session CRUD", () => {
  it("lists real records and supports rename, delete, and clear", async () => {
    installFetchMock();
    await completedRecord("First analysis");
    await completedRecord("Second analysis");
    renderApp("/projects");

    const firstCard = (await screen.findByRole("heading", { name: "First analysis" })).closest("article")!;
    fireEvent.click(within(firstCard).getByRole("button", { name: "Rename" }));
    fireEvent.change(within(firstCard).getByLabelText("Analysis name"), { target: { value: "Named session result" } });
    fireEvent.click(within(firstCard).getByRole("button", { name: "Save name" }));
    expect(await screen.findByRole("heading", { name: "Named session result" })).toBeInTheDocument();

    const secondCard = screen.getByRole("heading", { name: "Second analysis" }).closest("article")!;
    fireEvent.click(within(secondCard).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(secondCard).getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Second analysis" })).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Clear session analyses" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm clear" }));
    expect(await screen.findByRole("heading", { name: "No analyses in this session yet." })).toBeInTheDocument();
    expect(screen.queryByText("MOCK-001")).not.toBeInTheDocument();
  });
});
