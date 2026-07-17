import type {
  AnalyzeResponse,
  BiologicalResponse,
  EndpointSummary,
  ExplanationResult,
  ExploreLocateResult,
  InputValueType,
  LiteratureResponse,
  ParseResult,
  PathwaysResponse,
  Signature,
} from "../api/types";
import type { EndpointSignal } from "../hooks/useAnalyze";

export const GUEST_ANALYSIS_SCHEMA_VERSION = 1;
export const APPLICATION_VERSION = "0.0.0";

const SESSION_ID_KEY = "endoscan.guest-session-id.v1";
const ACTIVE_ANALYSIS_KEY = "endoscan.active-analysis-id.v1";
const DB_NAME = "endoscan-guest-workspace";
const STORE_NAME = "analyses";
const DB_VERSION = 1;

export type GuestAnalysisStatus =
  | "draft"
  | "validated"
  | "running"
  | "completed"
  | "partially_completed"
  | "failed";

export type AnalysisResultTab = "overview" | "biological" | "endpoint" | "similar" | "report";

export interface PreparedAnalysisInput {
  title: string;
  subtitle: string;
  filename?: string;
  kind: "file" | "paste" | "catalogue" | "reference";
  signature: Signature;
  parse: ParseResult;
  allowExtra: boolean;
  inputValueType: InputValueType;
  profileType?: string;
  valueTypeLabel?: string;
  referenceComparison?: string;
  aggregationWarning?: string;
  cellModels?: string[];
  experimentalContext?: string;
}

export interface GuestAnalysisRecord {
  id: string;
  session_id: string;
  schema_version: number;
  created_at: string;
  updated_at: string;
  last_opened_at: string;
  title: string;
  user_defined_name: string | null;
  status: GuestAnalysisStatus;
  input_source: PreparedAnalysisInput["kind"];
  input_filename: string | null;
  compound_identity: string | null;
  input_value_type: InputValueType;
  experimental_context: string | null;
  signature: Signature;
  parse_result: ParseResult;
  endpoint_compatibility: ParseResult["compatibility"];
  prepared_input: PreparedAnalysisInput;
  analysis_result: {
    signals: EndpointSignal[];
    summary: AnalyzeResponse["summary"] | null;
    run_error: string | null;
  } | null;
  biological_response: BiologicalResponse | null;
  endpoint_explanations: Record<string, ExplanationResult>;
  endpoint_pathways: Record<string, PathwaysResponse>;
  supporting_literature: Record<string, LiteratureResponse>;
  reference_placements: Record<string, ExploreLocateResult>;
  selected_endpoint: string | null;
  last_viewed_tab: AnalysisResultTab;
  model_registry_snapshot: EndpointSummary[];
  application_version: string;
  errors_by_capability: Record<string, string>;
}

export class GuestStorageError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = "GuestStorageError";
  }
}

function randomId(prefix: string): string {
  const value = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${value}`;
}

export function getGuestSessionId(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_ID_KEY);
    if (existing) return existing;
    const created = randomId("guest");
    sessionStorage.setItem(SESSION_ID_KEY, created);
    return created;
  } catch {
    return "guest-memory-session";
  }
}

export function getActiveAnalysisId(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_ANALYSIS_KEY);
  } catch {
    return null;
  }
}

export function setActiveAnalysisId(id: string | null): void {
  try {
    if (id) sessionStorage.setItem(ACTIVE_ANALYSIS_KEY, id);
    else sessionStorage.removeItem(ACTIVE_ANALYSIS_KEY);
  } catch {
    // The repository still works in memory when sessionStorage is unavailable.
  }
  dispatchWorkspaceChanged();
}

function dispatchWorkspaceChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("endoscan:workspace-changed"));
}

function clone<T>(value: T): T {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as T;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isSignature(value: unknown): value is Signature {
  return isObject(value) && Object.values(value).every((item) => typeof item === "number" && Number.isFinite(item));
}

const STATUSES: GuestAnalysisStatus[] = ["draft", "validated", "running", "completed", "partially_completed", "failed"];
const TABS: AnalysisResultTab[] = ["overview", "biological", "endpoint", "similar", "report"];
const SOURCES: PreparedAnalysisInput["kind"][] = ["file", "paste", "catalogue", "reference"];

function migrateRecord(value: unknown): GuestAnalysisRecord | null {
  if (!isObject(value)) return null;
  const version = typeof value.schema_version === "number" ? value.schema_version : 0;
  if (version > GUEST_ANALYSIS_SCHEMA_VERSION) return null;
  const migrated = version === 0
    ? { ...value, schema_version: 1, errors_by_capability: value.errors_by_capability ?? {} }
    : value;
  const prepared = isObject(migrated.prepared_input) ? migrated.prepared_input : null;
  const parse = isObject(migrated.parse_result) ? migrated.parse_result : null;
  if (
    typeof migrated.id !== "string"
    || typeof migrated.session_id !== "string"
    || typeof migrated.created_at !== "string"
    || typeof migrated.updated_at !== "string"
    || typeof migrated.last_opened_at !== "string"
    || typeof migrated.title !== "string"
    || !STATUSES.includes(migrated.status as GuestAnalysisStatus)
    || !SOURCES.includes(migrated.input_source as PreparedAnalysisInput["kind"])
    || !TABS.includes(migrated.last_viewed_tab as AnalysisResultTab)
    || !isSignature(migrated.signature)
    || !parse
    || typeof parse.ready !== "boolean"
    || !Array.isArray(parse.compatibility)
    || !isStringArray(parse.compatible_endpoint_ids)
    || !prepared
    || typeof prepared.title !== "string"
    || typeof prepared.subtitle !== "string"
    || !SOURCES.includes(prepared.kind as PreparedAnalysisInput["kind"])
    || !isSignature(prepared.signature)
    || !isObject(prepared.parse)
    || typeof prepared.allowExtra !== "boolean"
    || typeof prepared.inputValueType !== "string"
    || (migrated.analysis_result != null && (!isObject(migrated.analysis_result) || !Array.isArray(migrated.analysis_result.signals)))
    || !isObject(migrated.endpoint_explanations)
    || !isObject(migrated.endpoint_pathways)
    || !isObject(migrated.supporting_literature)
    || !isObject(migrated.reference_placements)
    || !isObject(migrated.errors_by_capability)
    || !Array.isArray(migrated.model_registry_snapshot)
  ) return null;
  return migrated as unknown as GuestAnalysisRecord;
}

class GuestAnalysisRepository {
  private readonly memory = new Map<string, GuestAnalysisRecord>();
  private readonly pendingCreates = new Map<string, Promise<GuestAnalysisRecord>>();
  private readonly importedRecordIds = new Map<string, string>();
  private dbPromise: Promise<IDBDatabase | null> | null = null;
  private durable = true;

  isDurable(): boolean {
    return this.durable;
  }

  async create(
    input: PreparedAnalysisInput,
    endpoints: EndpointSummary[],
    importKey?: string,
  ): Promise<GuestAnalysisRecord> {
    if (importKey) {
      const pending = this.pendingCreates.get(importKey);
      if (pending) return pending;
      const importedId = this.importedRecordIds.get(importKey);
      if (importedId) {
        const imported = await this.get(importedId);
        if (imported) return imported;
        this.importedRecordIds.delete(importKey);
      }
    }
    const now = new Date().toISOString();
    const record: GuestAnalysisRecord = {
      id: randomId("analysis"),
      session_id: getGuestSessionId(),
      schema_version: GUEST_ANALYSIS_SCHEMA_VERSION,
      created_at: now,
      updated_at: now,
      last_opened_at: now,
      title: input.title,
      user_defined_name: null,
      status: "validated",
      input_source: input.kind,
      input_filename: input.kind === "file" ? input.filename ?? input.title : null,
      compound_identity: input.kind === "catalogue" || input.kind === "reference" ? input.title : null,
      input_value_type: input.inputValueType,
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
