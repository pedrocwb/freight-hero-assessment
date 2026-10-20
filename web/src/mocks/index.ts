/**
 * Mock backend for `VITE_MOCK=1`: an in-memory world answering the API contract, seeded
 * with the assignment's story so every screen has data to lay out. It is not the agent:
 * messages and pings are recorded, but no run decides anything (their runs say so).
 * Brokers, SOP drafts and documents follow the wave 3 contracts (cards BRK, KNW, DOC, EVK).
 */

import type {
  ConversationItem,
  DocumentView,
  HeroTask,
  InboundMessage,
  LoadEvent,
  LoadSnapshot,
  LoadSummary,
  LocationPing,
  OutboxItem,
  RunRecord,
  SimState,
} from "../api";
import type { StreamEvent, StreamSource } from "../hooks/useEventStream";
import * as kb from "./brokers";
import { HttpError } from "./http";
import { SOP_SECTIONS, type MockSopSection } from "./sopData";
import { FIXTURES, SCENARIOS, SEED_NOW, TZ, documentEvent, loadFromMetadata, runRecord, seedLoads, type MockLoad } from "./story";

interface World {
  now: string;
  llmMode: string;
  loads: MockLoad[];
  done: Map<string, { done_at: string; note: string }>;
  seq: number;
  kb: kb.BrokerWorld;
}

function seed(): World {
  return { now: SEED_NOW, llmMode: "replay", loads: seedLoads(), done: new Map(), seq: 1, kb: kb.seedBrokerWorld() };
}

let world = seed();

// --- stream ------------------------------------------------------------------------------

const listeners = new Set<(event: StreamEvent) => void>();

export const mockStream: StreamSource = {
  subscribe(onEvent) {
    listeners.add(onEvent);
    return () => listeners.delete(onEvent);
  },
};

function emit(event: StreamEvent): void {
  // Delivered after the response, as a real stream would be.
  setTimeout(() => listeners.forEach((l) => l(event)), 30);
}

// --- read models ---------------------------------------------------------------------------

const cost = (runs: RunRecord[]) => runs.reduce((s, r) => s + r.usage.reduce((a, u) => a + u.cost_usd, 0), 0);

function summary(l: MockLoad): LoadSummary {
  return {
    load_id: l.load.load_id,
    broker_id: l.load.broker_id,
    broker_name: l.load.broker_name,
    stage: l.load.stage,
    delay_risk: l.load.delay_risk,
    open_obligations: l.obligations.filter((o) => o.status === "open").length,
    pending_timers: l.timers.filter((t) => t.status === "pending").length,
    cost_usd: cost(l.runs),
  };
}

function state(): SimState {
  const pending = world.loads
    .flatMap((l) => l.timers)
    .filter((t) => t.status === "pending")
    .map((t) => t.due_at)
    .sort((a, b) => Date.parse(a) - Date.parse(b));
  return {
    now: world.now,
    timezone: TZ,
    next_timer_due_at: pending[0] ?? null,
    llm_mode: world.llmMode,
    queue_size: 0,
    loads: world.loads.map(summary),
  };
}

function snapshot(l: MockLoad): LoadSnapshot {
  return { load: l.load, obligations: l.obligations, timers: l.timers, events: l.events, outbox: l.outbox, runs: l.runs };
}

const OUT_CHANNEL: Partial<Record<OutboxItem["kind"], ConversationItem["channel"]>> = {
  sms: "sms",
  email: "email",
  broker_slack: "slack",
};

function conversation(l: MockLoad, channel: ConversationItem["channel"]): ConversationItem[] {
  const role = (id: string | null) => l.load.contacts.find((c) => c.id === id)?.role ?? "unknown";
  const inbound: ConversationItem[] = l.events
    .filter((e) => e.type === "inbound_message" && e.channel === channel)
    .map((e) => ({ at: e.occurred_at, direction: "in", channel, who: e.sender_contact_id ?? "unknown", role: role(e.sender_contact_id), text: e.body }));
  const outbound: ConversationItem[] = l.outbox
    .filter((o) => OUT_CHANNEL[o.kind] === channel)
    .map((o) => ({
      at: o.created_at,
      direction: "out",
      channel,
      who: "agent",
      role: "agent",
      text: String(o.kind === "email" ? o.payload.body : o.payload.text),
      subject: o.kind === "email" ? (o.payload.subject as string | null) : null,
      status: o.status,
    }));
  return [...inbound, ...outbound].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}

function tasks(status: string): HeroTask[] {
  return world.loads
    .flatMap((l) => l.outbox.filter((o) => o.kind === "hero_task"))
    .map((o): HeroTask => {
      const done = world.done.get(o.outbox_id);
      return {
        task_id: o.outbox_id,
        load_id: o.load_id,
        title: String(o.payload.title),
        description: String(o.payload.description),
        urgent: Boolean(o.payload.urgent),
        created_at: o.created_at,
        status: done ? "done" : "open",
        done_at: done?.done_at ?? null,
        note: done?.note ?? null,
      };
    })
    .filter((t) => status === "all" || t.status === status);
}

/** The standard layer, then the broker's own published sections and its load-type sections. */
function effective(client: string, types: string[]) {
  const merged = new Map<string, { section_id: string; title: string; description: string; intents: string[]; stages: string[]; body: string; sources: string[] }>();
  const own = world.kb.published.get(client) ?? SOP_SECTIONS.filter((s) => s.layer === "client" && s.client_id === client);
  const scopes: MockSopSection[][] = [
    SOP_SECTIONS.filter((s) => s.layer === "standard"),
    own,
    ...types.map((t) => SOP_SECTIONS.filter((s) => s.layer === "load_type" && s.client_id === client && s.load_type === t)),
  ];
  for (const scope of scopes) {
    for (const s of scope) {
      const base = merged.get(s.section_id);
      if (s.mode === "extend" && s.layer !== "standard" && base) {
        merged.set(s.section_id, { ...base, body: `${base.body.trimEnd()}\n\n${s.body}`, sources: [...base.sources, s.source] });
      } else {
        merged.set(s.section_id, { section_id: s.section_id, title: s.title, description: s.description, intents: s.intents, stages: s.stages, body: s.body, sources: [s.source] });
      }
    }
  }
  return [...merged.values()].sort((a, b) => a.section_id.localeCompare(b.section_id));
}

function health() {
  const runs = world.loads.flatMap((l) => l.runs);
  const durations = runs
    .filter((r) => r.finished_at)
    .map((r) => (Date.parse(r.finished_at as string) - Date.parse(r.started_at)) / 1000)
    .sort((a, b) => a - b);
  const pct = (q: number) => (durations.length ? durations[Math.min(durations.length - 1, Math.floor((durations.length - 1) * q + 0.5))]! : null);
  const counts = { skipped: 0, noop: 0, acted: 0, escalated: 0, failed: 0 } as Record<RunRecord["status"], number>;
  runs.forEach((r) => (counts[r.status] += 1));
  const rates = Object.fromEntries(Object.entries(counts).map(([k, v]) => [k, runs.length ? v / runs.length : 0]));
  const loads = world.loads
    .map((l) => ({ load_id: l.load.load_id, runs: l.runs.length, cost_usd: cost(l.runs), budget_used: cost(l.runs), over_warning: false, over_budget: false }))
    .sort((a, b) => b.cost_usd - a.cost_usd);
  const total = cost(runs);
  const planned = runs.filter((r) => r.plan);
  const models = [...new Set(runs.flatMap((r) => r.usage.map((u) => u.model)))].sort();
  return {
    status: "warn",
    reasons: [{ check: "escalation_rate", level: "warn", message: "Mock data: the Hero task of load 481207 is still open" }],
    queue: { queued: 0, oldest_event_id: null, oldest_age_s: null, sla_s: 300, over_sla: false, dead_letters: 0 },
    runs: {
      total: runs.length,
      finished: durations.length,
      duration_p50_s: pct(0.5),
      duration_p95_s: pct(0.95),
      status_counts: counts,
      status_rates: rates,
      planned: planned.length,
      blocked_runs: planned.filter((r) => r.violations.length).length,
      high_severity_runs: 0,
      block_rate: planned.length ? planned.filter((r) => r.violations.length).length / planned.length : 0,
      models: models.map((m) => ({ model: m, calls: runs.flatMap((r) => r.usage).filter((u) => u.model === m).length, errors: 0, error_rate: 0 })),
    },
    cost: { budget_usd: 1, total_cost_usd: total, avg_cost_per_run_usd: runs.length ? total / runs.length : 0, loads },
    outbox: { pending: 0, failed: 0, stuck: 0, oldest_pending_age_s: null, failed_ids: [], stuck_ids: [] },
    settings: {
      BLOBS_DIR: "blobs",
      CASSETTES_DIR: "evals/cassettes",
      DATABASE_URL: "unset",
      DATABASE_URL_DIRECT: "unset",
      EVALS_DIR: "evals",
      LANGSMITH_API_KEY: "unset",
      LANGSMITH_PROJECT: "freight-agent",
      LANGSMITH_TRACING: "false",
      LLM_MODE: world.llmMode,
      MODELS_CONFIG: "config/models.yaml",
      OPENROUTER_API_KEY: "set",
      PERSISTENCE: "memory",
      SOPS_DIR: "sops",
      TELEGRAM_BOT_TOKEN: "unset",
    },
  };
}

// --- writes ------------------------------------------------------------------------------

function advance(minutes: number): void {
  world.now = toLocal(new Date(Date.parse(world.now) + minutes * 60000).toISOString());
  emit({ type: "clock", data: state() });
}

/** Keep the sim clock in the stop's offset, as the API does. */
function toLocal(iso: string): string {
  const ms = Date.parse(iso);
  const local = new Date(ms - 7 * 3600000).toISOString().slice(0, 19);
  return `${local}-07:00`;
}

function mockRun(l: MockLoad, event: LoadEvent): RunRecord {
  const run = runRecord(
    l.load.load_id,
    l.load.broker_id,
    {
      run_id: `run_mock_${world.seq++}`,
      event_id: event.event_id,
      started_at: event.occurred_at,
      status: "skipped",
      gate: null,
      sop_section_ids: [],
      plan: null,
      usage: [],
      skip_reason: "mock backend: the event is recorded, but no agent runs in mock mode",
    },
    event.occurred_at,
    world.kb.brokers.get(l.load.broker_id)?.config_version ?? 1,
  );
  l.runs.push(run);
  emit({ type: "run", data: run });
  emit({ type: "load", data: summary(l) });
  return run;
}

/**
 * `documents.process_pending`, first in a tick: each pending document is read as a POD
 * whose values match the load, except a file named like "exception", which needs review.
 */
function processDocuments(): void {
  for (const l of world.loads) {
    for (const doc of l.documents.filter((d) => d.status === "pending")) {
      const exception = /exception|damage|short/i.test(doc.filename);
      const po = /PO (\d+)/.exec(l.load.instructions)?.[1] ?? null;
      const receiver = l.load.stops.find((s) => s.type === "delivery")?.name ?? null;
      const processed: DocumentView = {
        ...doc,
        status: exception ? "needs_review" : "processed",
        type_id: "pod",
        confidence: exception ? 0.81 : 0.95,
        extracted: { receiver, signed_by: "J. Reyes", po_number: po, exceptions: exception ? "2 cases short, 1 crushed" : "none" },
        checks: [
          { field: "receiver", expected: receiver, found: receiver, ok: true },
          { field: "po_number", expected: po, found: po, ok: true },
          { field: "exceptions", expected: "none", found: exception ? "2 cases short, 1 crushed" : "none", ok: !exception },
        ],
        processed_at: world.now,
      };
      l.documents = l.documents.map((d) => (d.document_id === doc.document_id ? processed : d));
      const event = documentEvent(processed, world.now);
      l.events.push(event);
      mockRun(l, event);
    }
  }
}

// --- router ---------------------------------------------------------------------------------

function findLoad(id: string): MockLoad {
  const l = world.loads.find((x) => x.load.load_id === id);
  if (!l) throw new HttpError(404, `load ${id} not found`);
  return l;
}

const COLLECTIONS = ["loads", "tasks", "brokers", "documents"];
const SUBRESOURCES = ["conversation", "tms", "runs", "documents", "attachments"];

/** "GET /brokers/acme/sops/draft" reads as "GET /brokers/:id/sops/draft". */
function routeKey(method: string, parts: readonly string[]): string {
  const shape = parts.map((p, i) => (i > 0 && COLLECTIONS.includes(parts[i - 1]!) && !SUBRESOURCES.includes(p) ? ":id" : p));
  return `${method} /${shape.join("/")}`;
}

function suiteResults(b: { suite: string; mode: string; k: number; scenario?: string | null; cost_cap_usd?: number | null }) {
  const chosen = SCENARIOS.filter((s) => s.suite === b.suite && (!b.scenario || s.id === b.scenario));
  if (!chosen.length) throw new HttpError(404, `no scenario ${b.scenario ?? ""} in suite ${b.suite}`);
  let spent = 0;
  return chosen.map((s, i) => {
    const each = b.mode === "scripted" ? 0 : b.mode === "live" ? (0.0019 + (i % 4) * 0.0004) * b.k : 0;
    const capped = b.mode === "live" && b.cost_cap_usd !== null && b.cost_cap_usd !== undefined && spent + each > b.cost_cap_usd;
    if (capped) {
      return {
        scenario_id: s.id,
        passed: false,
        pass_k: 0,
        trials: 0,
        cost_usd: 0,
        failures: [{ step_index: null, check: "cost_cap", message: `not run: the cost cap of $${b.cost_cap_usd?.toFixed(2)} was reached` }],
      };
    }
    spent += each;
    const fails = b.mode === "replay" && s.id === "c3_relative_eta";
    return {
      scenario_id: s.id,
      passed: !fails,
      pass_k: fails ? 0 : 1,
      trials: b.k,
      cost_usd: each || (b.mode === "replay" ? 0.0011 + (i % 5) * 0.0002 : 0),
      failures: fails ? [{ step_index: 1, check: "expect.actions[0]", message: "expected a tms_note containing '07:45', found 'ETA 7:15 PM'" }] : [],
    };
  });
}

async function route(method: string, path: string, query: URLSearchParams, body: unknown): Promise<unknown> {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const key = routeKey(method, parts);
  const form = body instanceof FormData ? body : null;

  switch (key) {
    case "GET /sim/state":
      return state();
    case "POST /sim/reset": {
      const b = body as { start?: string; llm_mode?: string | null };
      world = { now: b.start || SEED_NOW, llmMode: b.llm_mode || world.llmMode, loads: [], done: new Map(), seq: 1, kb: world.kb };
      emit({ type: "clock", data: state() });
      return state();
    }
    case "POST /sim/advance": {
      const b = body as { minutes?: number; to?: string };
      const minutes = b.to ? Math.max(0, (Date.parse(b.to) - Date.parse(world.now)) / 60000) : Number(b.minutes ?? 0);
      if (!Number.isFinite(minutes) || minutes < 0) throw new HttpError(400, "minutes must be a positive number");
      advance(minutes);
      processDocuments();
      return { state: state(), runs: [] };
    }
    case "POST /sim/tick":
      processDocuments();
      return { state: state(), runs: [] };
    case "GET /loads":
      return world.loads.map(summary);
    case "POST /loads": {
      let load;
      try {
        load = loadFromMetadata(body as Record<string, unknown>);
      } catch (error) {
        throw new HttpError(400, error instanceof Error ? error.message : String(error));
      }
      if (world.loads.some((l) => l.load.load_id === load.load_id)) throw new HttpError(409, `load ${load.load_id} already exists`);
      const created: MockLoad = { load, obligations: [], timers: [], events: [], outbox: [], runs: [], documents: [] };
      world.loads.push(created);
      emit({ type: "load", data: summary(created) });
      return summary(created);
    }
    case "GET /loads/:id":
      return snapshot(findLoad(parts[1]!));
    case "GET /loads/:id/conversation":
      return conversation(findLoad(parts[1]!), (query.get("channel") ?? "sms") as ConversationItem["channel"]);
    case "GET /loads/:id/tms":
      return findLoad(parts[1]!)
        .outbox.filter((o) => o.kind === "tms_note")
        .map((o) => ({ at: o.created_at, text: String(o.payload.text) }));
    case "GET /loads/:id/runs":
      return findLoad(parts[1]!).runs;
    case "GET /loads/:id/documents":
      return findLoad(parts[1]!).documents;
    case "POST /loads/:id/attachments": {
      const l = findLoad(parts[1]!);
      const file = form?.get("file");
      if (!(file instanceof File)) throw new HttpError(422, "file: attach a file");
      const n = world.seq++;
      const attachment = { attachment_id: `att_mock_${n}`, filename: file.name, content_type: file.type || "application/octet-stream", ref: `blobs/mock_${n}` };
      const occurredAt = String(form?.get("occurred_at") || world.now);
      const event: LoadEvent = {
        event_id: `msg_mock_${n}`,
        load_id: l.load.load_id,
        type: "inbound_message",
        occurred_at: occurredAt,
        channel: (String(form?.get("channel") || "sms") as LoadEvent["channel"]),
        direction: "inbound",
        sender_contact_id: (form?.get("sender_contact_id") as string | null) ?? null,
        body: String(form?.get("body") ?? ""),
        attachments: [attachment],
        data: {},
      };
      const document: DocumentView = {
        document_id: `doc_mock_${n}`,
        load_id: l.load.load_id,
        attachment_id: attachment.attachment_id,
        broker_id: l.load.broker_id,
        filename: file.name,
        content_type: attachment.content_type,
        status: "pending",
        type_id: null,
        confidence: null,
        extracted: {},
        checks: [],
        text: "",
        pages: 1,
        received_at: occurredAt,
        processed_at: null,
      };
      l.events.push(event);
      l.documents.push(document);
      mockRun(l, event);
      return { attachment_id: attachment.attachment_id, document_id: document.document_id, event_id: event.event_id };
    }
    case "GET /documents/:id": {
      const doc = world.loads.flatMap((l) => l.documents).find((d) => d.document_id === parts[1]);
      if (!doc) throw new HttpError(404, `document ${parts[1]} not found`);
      return doc;
    }
    case "GET /fixtures":
      return FIXTURES;
    case "POST /v1/events": {
      const m = body as InboundMessage;
      const l = findLoad(m.load_id);
      if (!m.body?.trim()) throw new HttpError(422, "body: the message is empty");
      const event: LoadEvent = {
        event_id: m.message_id ?? `msg_mock_${world.seq++}`,
        load_id: m.load_id,
        type: "inbound_message",
        occurred_at: m.occurred_at,
        channel: m.channel,
        direction: "inbound",
        sender_contact_id: m.sender_contact_id,
        body: m.body,
        attachments: [],
        data: {},
      };
      l.events.push(event);
      mockRun(l, event);
      return { accepted: true, duplicate: false, event_id: event.event_id, reason: null };
    }
    case "POST /v1/pings": {
      const p = body as LocationPing;
      const l = findLoad(p.load_id);
      const previous = l.load.delay_risk;
      l.load = { ...l.load, delay_risk: p.delay_risk };
      const crosses = (previous === "high") !== (p.delay_risk === "high");
      if (crosses) {
        const event: LoadEvent = {
          event_id: `risk:${p.ping_id ?? `ping_mock_${world.seq++}`}`,
          load_id: p.load_id,
          type: "risk_changed",
          occurred_at: p.occurred_at,
          channel: "system",
          direction: "inbound",
          sender_contact_id: null,
          body: "",
          attachments: [],
          data: { previous, current: p.delay_risk },
        };
        l.events.push(event);
        mockRun(l, event);
      } else {
        emit({ type: "load", data: summary(l) });
      }
      return { accepted: true, duplicate: false, event_id: crosses ? `risk:${p.ping_id ?? "mock"}` : null, reason: crosses ? null : "no risk band change into or out of high" };
    }
    case "GET /hero/tasks":
      return tasks(query.get("status") ?? "open");
    case "POST /hero/tasks/:id/complete": {
      const id = parts[2]!;
      const task = tasks("all").find((t) => t.task_id === id);
      if (!task) throw new HttpError(404, `task ${id} not found`);
      if (task.status === "done") throw new HttpError(409, `task ${id} is already done`);
      world.done.set(id, { done_at: world.now, note: String((body as { note?: string }).note ?? "") });
      return tasks("all").find((t) => t.task_id === id);
    }
    case "GET /sops": {
      const client = query.get("client_id") ?? "";
      const types = (query.get("load_types") ?? "").split(",").filter(Boolean);
      const version = world.kb.brokers.get(client)?.sop_version ?? "c41f9a7e2b13";
      return { version, client_id: client, load_types: types, sections: effective(client, types) };
    }
    case "GET /sops/preview": {
      const types = (query.get("load_types") ?? "").split(",").filter(Boolean);
      const intent = query.get("intent") ?? "";
      const stage = query.get("stage") ?? "";
      const visible = effective(query.get("client_id") ?? "", types);
      return {
        sections: visible.filter((s) => (!s.intents.length || s.intents.includes(intent)) && (!s.stages.length || s.stages.includes(stage))),
        catalog: visible.map((s) => ({ section_id: s.section_id, title: s.title, description: s.description })),
      };
    }
    case "GET /sops/search": {
      const types = (query.get("load_types") ?? "").split(",").filter(Boolean);
      return kb.search(effective(query.get("broker_id") ?? "", types), query.get("q") ?? "", Number(query.get("k") || 5));
    }

    case "GET /brokers":
      return [...world.kb.brokers.values()];
    case "POST /brokers":
      return kb.createBroker(world.kb, body as Record<string, never>);
    case "GET /brokers/:id":
      return kb.findBroker(world.kb, parts[1]!);
    case "PUT /brokers/:id":
      return kb.updateBroker(world.kb, parts[1]!, body as Record<string, never>);
    case "GET /brokers/:id/versions":
      return kb.versionViews(world.kb, parts[1]!);
    case "POST /brokers/:id/rollout":
      return kb.setRollout(world.kb, parts[1]!, body as { percent?: number; previous_version?: number | null });
    case "POST /brokers/:id/sops": {
      await new Promise((r) => setTimeout(r, 600)); // compiling takes a moment
      const file = form?.get("file");
      return kb.uploadSop(world.kb, parts[1]!, file instanceof File ? file : null, world.now);
    }
    case "GET /brokers/:id/sops/draft":
      return kb.getDraft(world.kb, parts[1]!);
    case "PUT /brokers/:id/sops/draft":
      return kb.saveDraft(world.kb, parts[1]!, body as Record<string, never>, world.now);
    case "POST /brokers/:id/sops/publish":
      return kb.publishDraft(world.kb, parts[1]!, (body ?? {}) as { percent?: number }, world.now);
    case "GET /brokers/:id/sops/versions":
      return kb.sopVersions(world.kb, parts[1]!);
    case "POST /brokers/:id/evals/run": {
      const broker = kb.findBroker(world.kb, parts[1]!);
      const b = body as { suite: string; mode: string; k: number; cost_cap_usd: number };
      await new Promise((r) => setTimeout(r, 900));
      const results = suiteResults(b);
      return {
        suite: b.suite,
        mode: b.mode,
        k: b.k,
        broker_id: broker.broker_id,
        config_version: broker.config_version,
        sop_version: broker.sop_version,
        scenarios: results,
        langsmith_url: b.mode === "live" ? `https://smith.langchain.com/o/freight-hero/datasets/freight-agent-${b.suite}/compare?broker=${broker.broker_id}` : null,
      };
    }

    case "GET /evals/scenarios":
      return SCENARIOS;
    case "POST /evals/run": {
      const b = body as { suite: string; mode: string; k: number; scenario: string | null };
      await new Promise((r) => setTimeout(r, 700));
      return { suite: b.suite, mode: b.mode, k: b.k, scenarios: suiteResults(b) };
    }
    case "GET /health":
      return health();
    default:
      throw new HttpError(404, `no mock route for ${method} ${path}`);
  }
}

const CREATED = new Set(["POST /loads", "POST /brokers"]);

export async function mockFetch(input: string, init?: RequestInit): Promise<Response> {
  const url = new URL(input, "http://mock.local");
  const path = url.pathname.replace(/^\/api/, "");
  const method = (init?.method ?? "GET").toUpperCase();
  const raw = init?.body;
  const body = raw instanceof FormData ? raw : typeof raw === "string" && raw ? JSON.parse(raw) : undefined;
  await new Promise((r) => setTimeout(r, 40));
  try {
    const data = await route(method, path, url.searchParams, body);
    return new Response(JSON.stringify(data), { status: CREATED.has(`${method} ${path}`) ? 201 : 200, headers: { "Content-Type": "application/json" } });
  } catch (error) {
    const status = error instanceof HttpError ? error.status : 500;
    return new Response(JSON.stringify({ detail: error instanceof Error ? error.message : String(error) }), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }
}
