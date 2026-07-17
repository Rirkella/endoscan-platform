import { describe, expect, it } from "vitest";

import type { EndpointSummary, ParseResult } from "../api/types";
import {
  GUEST_ANALYSIS_SCHEMA_VERSION,
  getGuestSessionId,
  guestAnalysisRepository,
  type PreparedAnalysisInput,
} from "../session/guestAnalysis";
import endpointsFixture from "./fixtures/endpoints.json";
import parseFixture from "./fixtures/parse_ok.json";

function prepared(title = "Caffeic acid session analysis"): PreparedAnalysisInput {
  const parse = parseFixture as ParseResult;
  return {
    title,
    subtitle: "Repository contract fixture",
    kind: "paste",
    signature: parse.signature ?? { A1BG: 0.1 },
    parse,
    allowExtra: false,
    inputValueType: parse.input_value_type,
  };
}

describe("guest analysis repository", () => {
  it("creates, updates, renames, lists, deletes, and clears current-session records", async () => {
    const first = await guestAnalysisRepository.create(prepared("First"), endpointsFixture as EndpointSummary[]);
    const second = await guestAnalysisRepository.create(prepared("Second"), endpointsFixture as EndpointSummary[]);

    await guestAnalysisRepository.update(first.id, { status: "completed", last_viewed_tab: "endpoint" });
    await guestAnalysisRepository.rename(first.id, "Renamed analysis");

    const saved = await guestAnalysisRepository.get(first.id);
    expect(saved).toMatchObject({
      id: first.id,
      user_defined_name: "Renamed analysis",
      status: "completed",
      last_viewed_tab: "endpoint",
    });
    expect(await guestAnalysisRepository.list()).toHaveLength(2);

    await guestAnalysisRepository.delete(second.id);
    expect(await guestAnalysisRepository.list()).toHaveLength(1);
    await guestAnalysisRepository.clearCurrentSession();
    expect(await guestAnalysisRepository.list()).toEqual([]);
  });

  it("isolates analyses when sessionStorage represents a new browser tab", async () => {
    const originalSession = getGuestSessionId();
    const record = await guestAnalysisRepository.create(prepared(), endpointsFixture as EndpointSummary[]);

    sessionStorage.clear();

    expect(getGuestSessionId()).not.toBe(originalSession);
    expect(await guestAnalysisRepository.get(record.id)).toBeNull();
    expect(await guestAnalysisRepository.list()).toEqual([]);
  });

  it("deduplicates concurrent and repeated automatic imports from one navigation", async () => {
    const input = prepared("Exact reference import");
    const endpoints = endpointsFixture as EndpointSummary[];
    const importKey = "navigation-1:reference:ER:ZFMITUMMTDLWHR-UHFFFAOYSA-N";

    const [first, second] = await Promise.all([
      guestAnalysisRepository.create(input, endpoints, importKey),
      guestAnalysisRepository.create(input, endpoints, importKey),
    ]);
    const repeated = await guestAnalysisRepository.create(input, endpoints, importKey);

    expect(second.id).toBe(first.id);
    expect(repeated.id).toBe(first.id);
    expect(await guestAnalysisRepository.list()).toHaveLength(1);

    const newNavigation = await guestAnalysisRepository.create(
      input,
      endpoints,
      "navigation-2:reference:ER:ZFMITUMMTDLWHR-UHFFFAOYSA-N",
    );
    expect(newNavigation.id).not.toBe(first.id);
    expect(await guestAnalysisRepository.list()).toHaveLength(2);
  });

  it("migrates a supported older record and skips corrupt or future records", async () => {
    const record = await guestAnalysisRepository.create(prepared(), endpointsFixture as EndpointSummary[]);
    guestAnalysisRepository.__resetForTests();
    guestAnalysisRepository.__seedForTests({
      ...record,
      id: "legacy-analysis",
      schema_version: 0,
      errors_by_capability: undefined,
    });
    guestAnalysisRepository.__seedForTests({ id: "corrupt-analysis", session_id: record.session_id });
    guestAnalysisRepository.__seedForTests({
      ...record,
      id: "future-analysis",
      schema_version: GUEST_ANALYSIS_SCHEMA_VERSION + 1,
    });

    const saved = await guestAnalysisRepository.list();
    expect(saved).toHaveLength(1);
    expect(saved[0]).toMatchObject({
      id: "legacy-analysis",
      schema_version: GUEST_ANALYSIS_SCHEMA_VERSION,
      errors_by_capability: {},
    });
  });

  it("falls back to the in-memory mirror when IndexedDB is unavailable", async () => {
    const record = await guestAnalysisRepository.create(prepared(), endpointsFixture as EndpointSummary[]);

    expect(guestAnalysisRepository.isDurable()).toBe(false);
    expect(await guestAnalysisRepository.get(record.id)).toMatchObject({ id: record.id });
  });
});
