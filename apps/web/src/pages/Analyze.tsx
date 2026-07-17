// Analyze â€” the primary flow, ported from prototype-v2's stepped workspace (source â†’ validate â†’
// running â†’ result) and wired to the REAL API underneath:
//   source     : upload a signature/example file or select a verified measured public signature.
//                Every path uses multipart POST /signatures/parse; identity alone is never scored.
//   validate   : real gene coverage (from the parse preview) + real endpoint compatibility (/endpoints).
//   running    : shown while POST /analyze is in flight.
//   result     : overview (real scores/calls/thresholds/status), evidence (real /explain + pathways +
//                limitations), reference (real /explore/locate), report (PLANNED preview of exports).
// The endpoint set is always whatever /endpoints returns â€” never hardcoded ER/AR. Wording stays
// honest: endpoint signal score / model call (Active/Inactive), below-threshold is neutral, and the
// report/known-molecule areas are explicitly badged as not-yet-available.

import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import {
  predictionScore,
  type EndpointCompatibility,
  type EndpointSummary,
} from "../api/types";
import { PlannedBadge } from "../components/PlannedBadge";
import { ErrorNotice } from "../components/ErrorNotice";
import { EvidencePanel } from "../components/analyze/EvidencePanel";
import { BiologicalResponsePanel } from "../components/analyze/BiologicalResponsePanel";
import { ReferencePanel } from "../components/analyze/ReferencePanel";
import { SourceStep } from "../components/analyze/SourceStep";
import { ValidationStep } from "../components/analyze/ValidationStep";
import { type EndpointSignal, useAnalyze } from "../hooks/useAnalyze";
import { useAsync } from "../hooks/useAsync";
import {
  guestAnalysisRepository,
  setActiveAnalysisId,
  type GuestAnalysisRecord,
  type PreparedAnalysisInput,
} from "../session/guestAnalysis";

export type AnalyzeStep = "source" | "validate" | "running" | "result";
export type ResultTab = "overview" | "biological" | "endpoint" | "similar" | "report";

// What the user prepared, for display in validate/result headers. `signature` is the REAL payload.
export type PreparedInput = PreparedAnalysisInput;

export function Analyze() {
  const { analysisId } = useParams<{ analysisId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const catalogueSignatureId = searchParams.get("catalogue_signature");
  const referenceContext = searchParams.get("reference_context");
  const referenceCompound = searchParams.get("reference_compound");
  const catalogueHandled = useRef(false);
  const referenceHandled = useRef(false);
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const [record, setRecord] = useState<GuestAnalysisRecord | null>(null);
  const endpointList = endpoints.data ?? record?.model_registry_snapshot ?? [];
  const analyze = useAnalyze(endpointList);

  const [step, setStep] = useState<AnalyzeStep>("source");
  const [prepared, setPrepared] = useState<PreparedInput | null>(null);
  const [resultTab, setResultTab] = useState<ResultTab>("overview");
  const [runError, setRunError] = useState<unknown>(null);
  const [catalogueLoading, setCatalogueLoading] = useState(false);
  const [catalogueError, setCatalogueError] = useState<unknown>(null);
  const [restoring, setRestoring] = useState(Boolean(analysisId));
  const [unavailable, setUnavailable] = useState(false);
  const [storageWarning, setStorageWarning] = useState<string | null>(null);
  const [selectedEndpoint, setSelectedEndpoint] = useState<string | null>(null);

  useEffect(() => {
    if (!analysisId) {
      setRecord(null);
      setPrepared(null);
      setStep("source");
      setUnavailable(false);
      setRestoring(false);
      setResultTab("overview");
      setSelectedEndpoint(null);
      catalogueHandled.current = false;
      referenceHandled.current = false;
      return;
    }
    let alive = true;
    setRestoring(true);
    setUnavailable(false);
    void guestAnalysisRepository.get(analysisId)
      .then((saved) => {
        if (!alive) return;
        if (!saved) {
          setUnavailable(true);
          return;
        }
        const requestedTab = parseResultTab(searchParams.get("tab")) ?? saved.last_viewed_tab;
        const requestedEndpoint = searchParams.get("endpoint") ?? saved.selected_endpoint;
        setActiveAnalysisId(saved.id);
        setRecord(saved);
        setPrepared(saved.prepared_input);
        setResultTab(requestedTab);
        setSelectedEndpoint(requestedEndpoint);
        if (saved.analysis_result) {
          analyze.hydrate({
            signals: saved.analysis_result.signals,
            signature: saved.signature,
            summary: saved.analysis_result.summary,
          });
          setRunError(saved.analysis_result.run_error);
          setStep("result");
        } else {
          setStep(saved.status === "running" ? "validate" : "validate");
        }
        void guestAnalysisRepository.touch(saved.id, { tab: requestedTab, endpoint: requestedEndpoint })
          .catch(() => setStorageWarning("This analysis is available now but could not be saved in the session workspace."));
        const scroll = readScrollPosition(saved.id);
        if (scroll > 0) requestAnimationFrame(() => window.scrollTo({ top: scroll }));
      })
      .catch(() => {
        if (alive) setUnavailable(true);
      })
      .finally(() => {
        if (alive) setRestoring(false);
      });
    return () => {
      alive = false;
      saveScrollPosition(analysisId);
    };
    // Loading a saved result must not depend on live endpoint requests or rerun analysis.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisId]);

  useEffect(() => {
    if (analysisId || !catalogueSignatureId || catalogueHandled.current) return;
    catalogueHandled.current = true;
    setCatalogueLoading(true);
    setCatalogueError(null);
    void api
      .getCatalogueSignature(catalogueSignatureId)
      .then(async (detail) => {
        const catalogueFile = new File(
          [JSON.stringify(detail.signature)],
          `${detail.signature_id}.json`,
          { type: "application/json" },
        );
        const parsed = await api.parseSignature({
          file: catalogueFile,
          format: "json",
          input_value_type: "differential_zscore",
        });
        if (!parsed.signature) throw new Error("The selected measured signature could not be prepared.");
        onPrepared({
          title: detail.compound_name,
          subtitle: `${detail.dataset} ${detail.processing_level} Â· ${detail.cell_lines.join(" + ")}`,
          kind: "catalogue",
          signature: parsed.signature,
          parse: parsed,
          allowExtra: false,
          inputValueType: parsed.input_value_type,
        }, `${location.key}:catalogue:${catalogueSignatureId}`);
      })
      .catch(setCatalogueError)
      .finally(() => setCatalogueLoading(false));
  }, [analysisId, catalogueSignatureId, location.key]);

  useEffect(() => {
    if (analysisId || !referenceContext || !referenceCompound || referenceHandled.current) return;
    referenceHandled.current = true;
    setCatalogueLoading(true);
    setCatalogueError(null);
    void api
      .getReferenceSignature(referenceContext, referenceCompound)
      .then(async (detail) => {
        const referenceFile = new File(
          [JSON.stringify(detail.signature)],
          `${detail.context}-${detail.compound_id}-aggregated-reference.json`,
          { type: "application/json" },
        );
        const parsed = await api.parseSignature({
          file: referenceFile,
          format: "json",
          input_value_type: detail.value_type,
        });
        if (!parsed.signature) throw new Error("The reference support vector could not be prepared.");
        onPrepared({
          title: detail.preferred_name ?? detail.compound_id,
          subtitle: `${detail.profile_type} Â· ${detail.context} reference set`,
          kind: "reference",
          signature: parsed.signature,
          parse: parsed,
          allowExtra: false,
          inputValueType: parsed.input_value_type,
          profileType: detail.profile_type,
          valueTypeLabel: detail.value_type_label,
          referenceComparison: detail.reference_comparison,
          aggregationWarning: detail.aggregate_pathway_warning,
          cellModels: detail.cell_models,
          experimentalContext: detail.aggregation_description,
        }, `${location.key}:reference:${referenceContext}:${referenceCompound}`);
      })
      .catch(setCatalogueError)
      .finally(() => setCatalogueLoading(false));
  }, [analysisId, location.key, referenceContext, referenceCompound]);

  async function onPrepared(input: PreparedInput, importKey?: string) {
    setPrepared(input);
    setStep("validate");
    try {
      const saved = await guestAnalysisRepository.create(input, endpointList, importKey);
      if (!guestAnalysisRepository.isDurable()) {
        setStorageWarning("This analysis is available now but could not be saved in the session workspace.");
      }
      setRecord(saved);
      setActiveAnalysisId(saved.id);
      const target = `/analyze/${encodeURIComponent(saved.id)}`;
      window.history.replaceState(window.history.state, "", target);
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch (error) {
      setStorageWarning("This analysis is available now but could not be saved in the session workspace.");
      console.warn("Guest analysis persistence failed", error);
    }
  }

  async function onRun() {
    if (!prepared) return;
    setStep("running");
    setRunError(null);
    if (record) await persistPatch({ status: "running" });
    try {
      const completed = await analyze.run(
        prepared.signature,
        prepared.parse.compatible_endpoint_ids,
        prepared.allowExtra,
        prepared.inputValueType,
      );
      const status = completed.summary.status === "ok"
        ? "completed"
        : completed.summary.status === "partial"
          ? "partially_completed"
          : "failed";
      await persistPatch({
        status,
        analysis_result: { signals: completed.signals, summary: completed.summary, run_error: null },
      });
    } catch (e) {
      setRunError(e);
      await persistPatch({
        status: "failed",
        analysis_result: { signals: [], summary: null, run_error: errorMessage(e) },
      });
    } finally {
      changeTab("overview");
      setStep("result");
    }
  }

  async function persistPatch(patch: Partial<GuestAnalysisRecord>) {
    if (!record) return;
    setRecord((current) => current ? { ...current, ...patch } : current);
    try {
      const saved = await guestAnalysisRepository.update(record.id, patch);
      if (!guestAnalysisRepository.isDurable()) {
        setStorageWarning("This analysis is available now but could not be saved in the session workspace.");
      }
      setRecord(saved);
    } catch {
      setStorageWarning("This analysis is available now but could not be saved in the session workspace.");
    }
  }

  async function saveCapability(
    field: "endpoint_explanations" | "endpoint_pathways" | "supporting_literature" | "reference_placements",
    key: string,
    value: unknown,
  ) {
    if (!record) return;
    const latest = await guestAnalysisRepository.get(record.id).catch(() => null) ?? record;
    await persistPatch({ [field]: { ...latest[field], [key]: value } } as Partial<GuestAnalysisRecord>);
  }

  async function saveCapabilityError(key: string, error: unknown) {
    if (!record) return;
    const latest = await guestAnalysisRepository.get(record.id).catch(() => null) ?? record;
    await persistPatch({
      status: latest.status === "failed" ? "failed" : "partially_completed",
      errors_by_capability: { ...latest.errors_by_capability, [key]: errorMessage(error) },
    });
  }

  function changeTab(tab: ResultTab) {
    setResultTab(tab);
    if (!analysisId) return;
    const params = new URLSearchParams(location.search);
    params.set("tab", tab);
    if (selectedEndpoint) params.set("endpoint", selectedEndpoint);
    window.history.replaceState(window.history.state, "", `${location.pathname}?${params.toString()}`);
    void persistPatch({ last_viewed_tab: tab, last_opened_at: new Date().toISOString() });
  }

  function changeSelectedEndpoint(endpointId: string) {
    setSelectedEndpoint(endpointId);
    if (analysisId) {
      const params = new URLSearchParams(location.search);
      params.set("tab", "endpoint");
      params.set("endpoint", endpointId);
      window.history.replaceState(window.history.state, "", `${location.pathname}?${params.toString()}`);
      void persistPatch({ selected_endpoint: endpointId, last_viewed_tab: "endpoint" });
    }
  }

  if (restoring) return <section className="session-state"><h1>Restoring analysisâ€¦</h1><p>Loading the saved result from this browser session.</p></section>;
  if (unavailable) return <UnavailableAnalysis />;

  return (
    <div className="analyze-page">
      {storageWarning && <div className="session-storage-warning" role="status"><span>{storageWarning}</span><button type="button" onClick={() => setStorageWarning(null)}>Dismiss</button></div>}
      {step !== "result" && (
        <>
          <div className="page-header">
            <div>
              <p className="eyebrow">New screening</p>
              <h1>Analyze a gene-expression signature</h1>
              <p className="page-copy">
                Start with a measured biological response â€” upload your own or use a named real example file. Every
                input is checked before the models run. Results are experimental endpoint signals,
                not clinical, regulatory or safety conclusions.
              </p>
            </div>
          </div>
          <WorkflowSteps current={step} />
        </>
      )}

      {step === "source" && (
        <>
          {catalogueLoading && <p className="check-plain">Loading the selected measured signatureâ€¦</p>}
          {catalogueError != null && <ErrorNotice error={catalogueError} />}
          {!catalogueLoading && (
            <SourceStep onPrepared={onPrepared} endpointCount={endpointList.length} />
          )}
    8Ón÷¶‰žËkºwµçe_type: input.inputValueType,
      experimental_context: input.experimentalContext ?? null,
      signature: input.signature,
      parse_result: input.parse,
      endpoint_compatibility: input.parse.compatibility,
      prepared_input: input,
      analysis_result: null,
      biological_response: null,
      endpoint_explanations: {},
      endpoint_pathways: {},
      supporting_literature: {},
      reference_placements: {},
      selected_endpoint: input.parse.compatible_endpoint_ids[0] ?? null,
      last_viewed_tab: "overview",
      model_registry_snapshot: endpoints,
      application_version: APPLICATION_VERSION,
      errors_by_capability: {},
    };
    const saving = this.put(record)
      .then((saved) => {
        if (importKey) this.importedRecordIds.set(importKey, saved.id);
        return saved;
      })
      .finally(() => {
        if (importKey) this.pendingCreates.delete(importKey);
      });
    if (importKey) this.pendingCreates.set(importKey, saving);
    return saving;
  }

  async update(id: string, patch: Partial<GuestAnalysisRecord>): Promise<GuestAnalysisRecord> {
    const existing = await this.get(id);
    if (!existing) throw new GuestStorageError("The saved analysis no longer exists.");
    return this.put({ ...existing, ...clone(patch), id, updated_at: new Date().toISOString() });
  }

  async rename(id: string, name: string): Promise<GuestAnalysisRecord> {
    return this.update(id, { user_defined_name: name.trim() || null });
  }

  async touch(id: string, state?: { tab?: AnalysisResultTab; endpoint?: string | null }): Promise<GuestAnalysisRecord> {
    return this.update(id, {
      last_opened_at: new Date().toISOString(),
      ...(state?.tab ? { last_viewed_tab: state.tab } : {}),
      ...(state && "endpoint" in state ? { selected_endpoint: state.endpoint ?? null } : {}),
    });
  }

  async get(id: string): Promise<GuestAnalysisRecord | null> {
    const cached = this.memory.get(id);
    if (cached) return cached.session_id === getGuestSessionId() ? clone(cached) : null;
    const db = await this.openDatabase();
    if (!db) return null;
    try {
      const value = await requestResult(db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(id));
      const record = migrateRecord(value);
      if (!record || record.session_id !== getGuestSessionId()) return null;
      this.memory.set(record.id, record);
      return clone(record);
    } catch (error) {
      throw new GuestStorageError("Saved analyses could not be read from this browser.", error);
    }
  }

  async list(): Promise<GuestAnalysisRecord[]> {
    const sessionId = getGuestSessionId();
    const db = await this.openDatabase();
    let values: unknown[] = [...this.memory.values()];
    if (db) {
      try {
        values = await requestResult(db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).getAll());
      } catch (error) {
        throw new GuestStorageError("Saved analyses could not be listed in this browser.", error);
      }
    }
    const records = values
      .map(migrateRecord)
      .filter((item): item is GuestAnalysisRecord => item != null && item.session_id === sessionId)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    records.forEach((item) => this.memory.set(item.id, item));
    return clone(records);
  }

  async delete(id: string): Promise<void> {
    this.memory.delete(id);
    const db = await this.openDatabase();
    if (db) await transactionDone(db.transaction(STORE_NAME, "readwrite"), (store) => store.delete(id));
    if (getActiveAnalysisId() === id) setActiveAnalysisId(null);
    dispatchWorkspaceChanged();
  }

  async clearCurrentSession(): Promise<void> {
    const records = await this.list();
    const db = await this.openDatabase();
    records.forEach((record) => this.memory.delete(record.id));
    if (db) {
      const tx = db.transaction(STORE_NAME, "readwrite");
      records.forEach((record) => tx.objectStore(STORE_NAME).delete(record.id));
      await transactionComplete(tx);
    }
    setActiveAnalysisId(null);
    dispatchWorkspaceChanged();
  }

  async put(record: GuestAnalysisRecord): Promise<GuestAnalysisRecord> {
    const validated = migrateRecord(record);
    if (!validated) throw new GuestStorageError("The analysis record did not pass runtime validation.");
    this.memory.set(validated.id, clone(validated));
    const db = await this.openDatabase();
    if (db) {
      try {
        await transactionDone(db.transaction(STORE_NAME, "readwrite"), (store) => store.put(validated));
      } catch (error) {
        this.durable = false;
        dispatchWorkspaceChanged();
        console.warn("Guest analysis is continuing with in-memory storage after IndexedDB failed.", error);
        return clone(validated);
      }
    }
    dispatchWorkspaceChanged();
    return clone(validated);
  }

  __resetForTests(): void {
    this.memory.clear();
    this.pendingCreates.clear();
    this.importedRecordIds.clear();
    this.dbPromise = null;
    this.durable = true;
  }

  __seedForTests(value: unknown): void {
    if (isObject(value) && typeof value.id === "string") {
      this.memory.set(value.id, value as unknown as GuestAnalysisRecord);
    }
  }

  private openDatabase(): Promise<IDBDatabase | null> {
    if (this.dbPromise) return this.dbPromise;
    this.dbPromise = new Promise((resolve) => {
      if (typeof indexedDB === "undefined") {
        this.durable = false;
        return resolve(null);
      }
      try {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
            store.createIndex("session_id", "session_id", { unique: false });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => { this.durable = false; resolve(null); };
        request.onblocked = () => { this.durable = false; resolve(null); };
      } catch {
        this.durable = false;
        resolve(null);
      }
    });
    return this.dbPromise;
  }
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transactionComplete(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

async function transactionDone(
  tx: IDBTransaction,
  operation: (store: IDBObjectStore) => IDBRequest,
): Promise<void> {
  operation(tx.objectStore(STORE_NAME));
  await transactionComplete(tx);
}

export const guestAnalysisRepository = new GuestAnalysisRepository();
