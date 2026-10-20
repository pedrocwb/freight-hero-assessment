/**
 * Typed client for the platform API.
 *
 * The shapes mirror the API contract (base path `/api`) and the Python domain models it
 * serializes with `model_dump(mode="json")`: datetimes are ISO 8601 strings with an
 * offset, tuples are arrays, enums are their string values. Computed properties of the
 * Python models (such as `RunRecord.cost_usd`) are not serialized, so helpers here derive
 * them.
 *
 * `createApi` takes the fetch function, so tests check the requests it builds without a
 * network.
 */

// --- domain shapes -------------------------------------------------------------------

export type Stage =
  | "dispatched"
  | "to_pickup"
  | "at_pickup"
  | "to_delivery"
  | "at_delivery"
  | "delivered";
export const STAGES: readonly Stage[] = [
  "dispatched",
  "to_pickup",
  "at_pickup",
  "to_delivery",
  "at_delivery",
  "delivered",
];

export type DelayRisk = "low" | "medium" | "high";
export type Role = "driver" | "dispatcher" | "broker";
export type Channel = "sms" | "email" | "slack" | "system" | "hero";
export type ConversationChannel = "sms" | "email" | "slack";

/**
 * An event intent id. The catalog is data: each broker lists its intents
 * (`GET /api/brokers/{id}`), so the page never hard-codes them.
 */
export type Intent = string;

export interface Contact {
  id: string;
  role: Role;
  channels: Channel[];
}

export interface Appointment {
  kind: "fixed" | "window";
  at: string | null;
  start: string | null;
  end: string | null;
}

export interface Stop {
  sequence: number;
  type: "pickup" | "delivery";
  name: string;
  address: string;
  timezone: string;
  appointment: Appointment;
}

export interface Load {
  load_id: string;
  broker_id: string;
  broker_name: string;
  shipper: string;
  carrier_name: string;
  mc_number: string;
  commodity: string;
  stage: Stage;
  delay_risk: DelayRisk;
  contacts: Contact[];
  stops: Stop[];
  instructions: string;
  load_types: string[];
  current_stop_sequence: number | null;
}

export type ObligationStatus = "open" | "satisfied" | "exhausted" | "cancelled";

export interface Obligation {
  obligation_id: string;
  load_id: string;
  /** An `ObligationDef.kind` of the load's broker: "eta_request", "pod", ... */
  kind: string;
  status: ObligationStatus;
  attempts: number;
  opened_at: string;
  last_action_at: string | null;
  deferred_until: string | null;
  deferrals_used: number;
  active_timer_id: string | null;
  sop_version: string | null;
}

export type TimerStatus = "pending" | "fired" | "superseded" | "cancelled";

export interface Timer {
  timer_id: string;
  load_id: string;
  obligation_id: string;
  due_at: string;
  created_at: string;
  status: TimerStatus;
  reason: string;
}

export type EventType =
  | "inbound_message"
  | "risk_changed"
  | "timer_due"
  | "attachment_received"
  | "hero_update"
  | "agent_action"
  | "document_processed";

export interface Attachment {
  attachment_id: string;
  filename: string;
  content_type: string;
  ref: string;
}

/** An entry of the load's event log (`domain.events.Event`). */
export interface LoadEvent {
  event_id: string;
  load_id: string;
  type: EventType;
  occurred_at: string;
  channel: Channel;
  direction: "inbound" | "outbound";
  sender_contact_id: string | null;
  body: string;
  attachments: Attachment[];
  data: Record<string, unknown>;
}

export type OutboxKind =
  | "sms"
  | "email"
  | "broker_slack"
  | "tms_note"
  | "hero_task"
  | "internal_slack"
  | "stage_update";

export interface OutboxItem {
  outbox_id: string;
  idempotency_key: string;
  run_id: string;
  load_id: string;
  kind: OutboxKind;
  payload: Record<string, unknown>;
  created_at: string;
  status: "pending" | "sent" | "failed";
  attempts: number;
  last_error: string | null;
}

export interface GateDecision {
  intent: Intent;
  act: boolean;
  reason: string;
}

export type Purpose = "eta_request" | "reply" | "escalation" | "info";

/**
 * `requests` names the obligation kind a message asks for ("eta_request", "pod"); the API
 * sets "eta_request" when `purpose` is `eta_request`. Records from before wave 3 omit it.
 */
export type Action =
  | { type: "send_sms"; contact_id: string; text: string; purpose: Purpose; requests?: string | null }
  | {
      type: "send_email";
      to_contact_ids: string[];
      body: string;
      subject: string | null;
      purpose: Purpose;
      requests?: string | null;
    }
  | { type: "post_broker_slack"; text: string }
  | { type: "write_tms_note"; text: string }
  | { type: "create_hero_task"; title: string; description: string; urgent: boolean }
  | { type: "post_internal_slack"; text: string }
  | { type: "update_stage"; stage: Stage }
  | { type: "schedule_timer"; kind: string; due_in_minutes: number }
  | { type: "defer_obligation"; kind: string; minutes: number }
  | {
      type: "close_obligation";
      kind: string;
      status: "satisfied" | "exhausted" | "cancelled";
    }
  | { type: "open_obligation"; kind: string };

export interface Plan {
  actions: Action[];
  rationale: string;
}

export interface Violation {
  rule: string;
  action_index: number;
  message: string;
  severity: "low" | "high";
}

export interface Usage {
  role: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  latency_ms: number;
}

export type RunStatus = "skipped" | "noop" | "acted" | "escalated" | "failed";

/**
 * One tool call of the agent loop (`domain.runs.TrajectoryStep`, card AGT). `accepted` is
 * null for a read tool, true or false for an action tool the rules checked at the call.
 */
export interface TrajectoryStep {
  tool: string;
  args: Record<string, unknown>;
  result: string;
  accepted: boolean | null;
  cost_usd: number;
}

export interface RunRecord {
  run_id: string;
  event_id: string;
  load_id: string;
  started_at: string;
  finished_at: string | null;
  status: RunStatus;
  gate: GateDecision | null;
  sop_section_ids: string[];
  plan: Plan | null;
  violations: Violation[];
  usage: Usage[];
  skip_reason: string | null;
  error: string | null;
  trace_url: string | null;
  sop_version?: string | null;
  // Card AGT. Records stored before wave 3 read back with these defaults.
  intent: Intent | null;
  broker_id: string | null;
  broker_config_version: number | null;
  trajectory: TrajectoryStep[];
  sections_searched: string[];
  rejections: number;
}

// --- API read models -----------------------------------------------------------------

export interface LoadSummary {
  load_id: string;
  broker_id: string;
  broker_name: string;
  stage: Stage;
  delay_risk: DelayRisk;
  open_obligations: number;
  pending_timers: number;
  cost_usd: number;
}

export interface SimState {
  now: string;
  timezone: string;
  next_timer_due_at: string | null;
  llm_mode: string;
  queue_size: number;
  loads: LoadSummary[];
}

export interface AdvanceResult {
  state: SimState;
  runs: RunRecord[];
}

export interface LoadSnapshot {
  load: Load;
  obligations: Obligation[];
  timers: Timer[];
  events: LoadEvent[];
  outbox: OutboxItem[];
  runs: RunRecord[];
}

/** Assignment-shaped load metadata, as the fixtures hold it. Kept loose on purpose. */
export type LoadMetadata = Record<string, unknown> & { load_id?: string };

export interface Fixture {
  name: string;
  metadata: LoadMetadata;
}

export interface ConversationItem {
  at: string;
  direction: "in" | "out";
  channel: ConversationChannel;
  who: string; // contact id, or "agent"
  role: string;
  text: string;
  subject?: string | null;
  status?: string | null;
}

export interface TmsNote {
  at: string;
  text: string;
}

export interface HeroTask {
  task_id: string;
  load_id: string;
  title: string;
  description: string;
  urgent: boolean;
  created_at: string;
  status: "open" | "done";
  done_at?: string | null;
  note?: string | null;
}

export type TaskFilter = "open" | "done" | "all";

export interface ResolvedSection {
  section_id: string;
  title: string;
  description: string;
  intents: Intent[];
  stages: Stage[];
  body: string;
  sources: string[];
}

export interface CatalogEntry {
  section_id: string;
  title: string;
  description: string;
}

export interface SopSections {
  version: string;
  sections: ResolvedSection[];
}

export interface ResolvedSop {
  sections: ResolvedSection[];
  catalog: CatalogEntry[];
}

// --- knowledge (api/routes/sops.py) ----------------------------------------------------

/** `SearchHitView`; `score` only orders the hits of one search. */
export interface SearchHit {
  section_id: string;
  title: string;
  description: string;
  score: number;
}

// --- documents (card DOC: api/routes/documents.py) --------------------------------------

export type DocumentStatus = "pending" | "processed" | "needs_review" | "failed";

/** One checked value: what the load says against what the document says. */
export interface FieldCheck {
  field: string;
  expected: string | null;
  found: string | null;
  ok: boolean;
}

/** `DocumentView`: a load's document as the pipeline left it (the blob reference stays internal). */
export interface DocumentView {
  document_id: string;
  load_id: string;
  attachment_id: string;
  broker_id: string;
  filename: string;
  content_type: string;
  status: DocumentStatus;
  type_id: string | null;
  confidence: number | null;
  extracted: Record<string, unknown>;
  checks: FieldCheck[];
  text: string;
  pages: number;
  received_at: string;
  processed_at: string | null;
}

/** The form fields of `POST /api/loads/{id}/attachments` besides the file. */
export interface AttachmentUpload {
  file: Blob;
  filename?: string;
  channel: ConversationChannel;
  senderContactId: string | null;
  body: string;
  at: string;
}

export interface AttachmentUploadResult {
  attachment_id: string;
  document_id: string;
  event_id: string;
}

// --- ingress ---------------------------------------------------------------------------

/** `InboundMessage`; the API fills `message_id` when it is left out. */
export interface InboundMessage {
  message_id?: string;
  load_id: string;
  channel: ConversationChannel;
  sender_contact_id: string | null;
  body: string;
  occurred_at: string;
  attachments?: Attachment[];
}

/** `LocationPing`; the API fills `ping_id` when it is left out. */
export interface LocationPing {
  ping_id?: string;
  load_id: string;
  lat: number;
  lon: number;
  occurred_at: string;
  delay_risk: DelayRisk;
}

export interface IngestResult {
  accepted: boolean;
  duplicate: boolean;
  event_id: string | null;
  reason: string | null;
}

export interface ResetRequest {
  start: string;
  llm_mode: "live" | "replay" | null;
}

export type AdvanceBy = { minutes: number } | { to: string };

// --- request building ----------------------------------------------------------------

export type QueryValue = string | number | boolean | null | undefined | readonly string[];
export type Query = Record<string, QueryValue>;

/** Path plus query string. Empty values are left out; a list is sent comma-separated. */
export function buildUrl(base: string, path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      const items = value.map((v) => v.trim()).filter(Boolean);
      if (items.length) params.set(key, items.join(","));
      continue;
    }
    params.set(key, String(value));
  }
  const qs = params.toString();
  return `${base}${path}${qs ? `?${qs}` : ""}`;
}

export interface RequestSpec {
  method: "GET" | "POST" | "PUT";
  path: string;
  query?: Query;
  body?: unknown;
  /** A multipart body (file uploads); the browser sets its content type and boundary. */
  form?: FormData;
}

export function buildRequest(base: string, spec: RequestSpec): { url: string; init: RequestInit } {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method: spec.method, headers };
  if (spec.form !== undefined) {
    init.body = spec.form;
  } else if (spec.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(spec.body);
  }
  return { url: buildUrl(base, spec.path, spec.query), init };
}

/** The multipart form of an attachment upload: the file plus the message it came with. */
export function attachmentForm(upload: AttachmentUpload): FormData {
  const form = new FormData();
  const name = upload.filename ?? (upload.file instanceof File ? upload.file.name : "attachment");
  form.append("file", upload.file, name);
  form.append("channel", upload.channel);
  if (upload.senderContactId) form.append("sender_contact_id", upload.senderContactId);
  form.append("body", upload.body.trim());
  form.append("occurred_at", upload.at);
  return form;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly path: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/**
 * The human message of an error response. The API sends `{"detail": "..."}`; FastAPI's
 * own validation errors send `{"detail": [{"loc": [...], "msg": "..."}]}`.
 */
export function errorDetail(status: number, payload: unknown, fallback = ""): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail
        .map((entry) => {
          if (entry && typeof entry === "object") {
            const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
            const where = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
            return where ? `${where}: ${String(msg)}` : String(msg);
          }
          return String(entry);
        })
        .join("; ");
    }
  }
  const text = typeof payload === "string" ? payload.trim() : "";
  if (text && text.length < 300 && !text.startsWith("<")) return text;
  return fallback || `Request failed with status ${status}`;
}

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

/** The inbound message a person writes in one of the role composers. */
export function inboundMessage(args: {
  loadId: string;
  channel: ConversationChannel;
  senderContactId: string | null;
  body: string;
  at: string;
}): InboundMessage {
  return {
    load_id: args.loadId,
    channel: args.channel,
    sender_contact_id: args.senderContactId,
    body: args.body.trim(),
    occurred_at: args.at,
  };
}

export function createApi(fetcher: Fetcher, base = "/api") {
  async function call<T>(spec: RequestSpec): Promise<T> {
    const { url, init } = buildRequest(base, spec);
    let response: Response;
    try {
      response = await fetcher(url, init);
    } catch {
      throw new ApiError(0, `Cannot reach the API at ${base} (is it running on :8000?)`, spec.path);
    }
    const text = await response.text();
    let payload: unknown = undefined;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    if (!response.ok) {
      throw new ApiError(response.status, errorDetail(response.status, payload), spec.path);
    }
    return payload as T;
  }

  const load = (id: string) => `/loads/${encodeURIComponent(id)}`;

  return {
    streamUrl: `${base}/stream`,

    simState: () => call<SimState>({ method: "GET", path: "/sim/state" }),
    resetSim: (body: ResetRequest) =>
      call<SimState>({ method: "POST", path: "/sim/reset", body }),
    advance: (by: AdvanceBy) =>
      call<AdvanceResult>({ method: "POST", path: "/sim/advance", body: by }),
    tick: () => call<AdvanceResult>({ method: "POST", path: "/sim/tick" }),

    listLoads: () => call<LoadSummary[]>({ method: "GET", path: "/loads" }),
    getLoad: (loadId: string) => call<LoadSnapshot>({ method: "GET", path: load(loadId) }),
    createLoad: (metadata: LoadMetadata) =>
      call<LoadSummary>({ method: "POST", path: "/loads", body: metadata }),
    fixtures: () => call<Fixture[]>({ method: "GET", path: "/fixtures" }),

    sendMessage: (message: InboundMessage, latencySeconds?: number) =>
      call<IngestResult>({
        method: "POST",
        path: "/v1/events",
        query: { latency_seconds: latencySeconds },
        body: message,
      }),
    sendPing: (ping: LocationPing, latencySeconds?: number) =>
      call<IngestResult>({
        method: "POST",
        path: "/v1/pings",
        query: { latency_seconds: latencySeconds },
        body: ping,
      }),

    conversation: (loadId: string, channel: ConversationChannel) =>
      call<ConversationItem[]>({
        method: "GET",
        path: `${load(loadId)}/conversation`,
        query: { channel },
      }),
    tmsNotes: (loadId: string) => call<TmsNote[]>({ method: "GET", path: `${load(loadId)}/tms` }),

    heroTasks: (status: TaskFilter) =>
      call<HeroTask[]>({ method: "GET", path: "/hero/tasks", query: { status } }),
    completeHeroTask: (taskId: string, note: string) =>
      call<HeroTask>({
        method: "POST",
        path: `/hero/tasks/${encodeURIComponent(taskId)}/complete`,
        body: { note },
      }),

    runs: (loadId: string) => call<RunRecord[]>({ method: "GET", path: `${load(loadId)}/runs` }),
    sops: (clientId: string, loadTypes: readonly string[]) =>
      call<SopSections>({
        method: "GET",
        path: "/sops",
        query: { client_id: clientId, load_types: loadTypes },
      }),
    sopPreview: (args: {
      clientId: string;
      loadTypes: readonly string[];
      intent: Intent;
      stage: Stage;
    }) =>
      call<ResolvedSop>({
        method: "GET",
        path: "/sops/preview",
        query: {
          client_id: args.clientId,
          load_types: args.loadTypes,
          intent: args.intent,
          stage: args.stage,
        },
      }),

    /** Card KNW: hybrid search over the sections a broker's loads of these types see. */
    searchSops: (args: { brokerId: string; loadTypes: readonly string[]; q: string; k?: number }) =>
      call<SearchHit[]>({
        method: "GET",
        path: "/sops/search",
        query: { q: args.q.trim(), broker_id: args.brokerId, load_types: args.loadTypes, k: args.k },
      }),

    // Card DOC: attachments and the documents the pipeline made of them.
    uploadAttachment: (loadId: string, upload: AttachmentUpload, latencySeconds?: number) =>
      call<AttachmentUploadResult>({
        method: "POST",
        path: `${load(loadId)}/attachments`,
        query: { latency_seconds: latencySeconds },
        form: attachmentForm(upload),
      }),
    loadDocuments: (loadId: string) =>
      call<DocumentView[]>({ method: "GET", path: `${load(loadId)}/documents` }),
    getDocument: (documentId: string) =>
      call<DocumentView>({ method: "GET", path: `/documents/${encodeURIComponent(documentId)}` }),
  };
}

export type Api = ReturnType<typeof createApi>;

export const api = createApi((input, init) => fetch(input, init));

// --- derived values ----------------------------------------------------------------------

export function runCost(run: Pick<RunRecord, "usage">): number {
  return run.usage.reduce((sum, u) => sum + (u.cost_usd || 0), 0);
}

export function runTokens(run: Pick<RunRecord, "usage">): {
  input: number;
  output: number;
  cached: number;
} {
  return run.usage.reduce(
    (sum, u) => ({
      input: sum.input + (u.input_tokens || 0),
      output: sum.output + (u.output_tokens || 0),
      cached: sum.cached + (u.cached_tokens || 0),
    }),
    { input: 0, output: 0, cached: 0 },
  );
}

export function runLatencyMs(run: Pick<RunRecord, "usage">): number {
  return run.usage.reduce((sum, u) => sum + (u.latency_ms || 0), 0);
}
