/**
 * Mock backend for `VITE_MOCK=1`: an in-memory world answering the API contract, seeded
 * with the assignment's story so every screen has data to lay out. It is not the agent:
 * messages and pings are recorded, but no run decides anything (their runs say so).
 */

import type {
  ConversationItem,
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
import { SOP_SECTIONS, type MockSopSection } from "./sopData";
import { FIXTURES, SCENARIOS, SEED_NOW, TZ, loadFromMetadata, seedLoads, type MockLoad } from "./story";

interface World {
  now: string;
  llmMode: string;
  loads: MockLoad[];
  done: Map<string, { done_at: string; note: string }>;
  seq: number;
}

function seed(): World {
  return { now: SEED_NOW, llmMode: "replay", loads: seedLoads(), done: new Map(), seq: 1 };
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

function effective(client: string, types: string[]) {
  const merged = new Map<string, { section_id: string; title: string; description: string; intents: string[]; stages: string[]; body: string; sources: string[] }>();
  const scopes: MockSopSection[][] = [
    SOP_SECTIONS.filter((s) => s.layer === "standard"),
    SOP_SECTIONS.filter((s) => s.layer === "client" && s.client_id === client),
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
  const run: RunRecord = {
    run_id: `run_mock_${world.seq++}`,
    event_id: event.event_id,
    load_id: l.load.load_id,
    started_at: event.occurred_at,
    finished_at: event.occurred_at,
    status: "skipped",
    gate: null,
    sop_section_ids: [],
    plan: null,
    violations: [],
    usage: [],
    skip_reason: "mock backend: the event is recorded, but no agent runs in mock mode",
    error: null,
    trace_url: null,
  };
  l.runs.push(run);
  emit({ type: "run", data: run });
  emit({ type: "load", data: summary(l) });
  return run;
}

// --- router ---------------------------------------------------------------------------------

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function findLoad(id: string): MockLoad {
  const l = world.loads.find((x) => x.load.load_id === id);
  if (!l) throw new HttpError(404, `load ${id} not found`);
  return l;
}

async function route(method: string, path: string, query: URLSearchParams, body: unknown): Promise<unknown> {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const key = `${method} /${parts.map((p, i) => (i > 0 && ["loads", "tasks"].includes(parts[i - 1]!) && !["conversation", "tms", "runs"].includes(p) ? ":id" : p)).join("/")}`;

  switch (key) {
    case "GET /sim/state":
      return state();
    case "POST /sim/reset": {
      const b = body as { start?: string; llm_mode?: string | null };
      world = { now: b.start || SEED_NOW, llmMode: b.llm_mode || world.llmMode, loads: [], done: new Map(), seq: 1 };
      emit({ type: "clock", data: state() });
      return state();
    }
    case "POST /sim/advance": {
      const b = body as { minutes?: number; to?: string };
      const minutes = b.to ? Math.max(0, (Date.parse(b.to) - Date.parse(world.now)) / 60000) : Number(b.minutes ?? 0);
      if (!Number.isFinite(minutes) || minutes < 0) throw new HttpError(400, "minutes must be a positive number");
      advance(minutes);
      return { state: state(), runs: [] };
    }
    case "POST /sim/tick":
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
      const created: MockLoad = { load, obligations: [], timers: [], events: [], outbox: [], runs: [] };
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
      const types = (query.get("load_types") ?? "").split(",").filter(Boolean);
      return { version: "c41f9a7e2b13d8a05f6e", sections: effective(query.get("client_id") ?? "", types) };
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
    case "GET /evals/scenarios":
      return SCENARIOS;
    case "POST /evals/run": {
      const b = body as { suite: string; mode: string; k: number; scenario: string | null };
      await new Promise((r) => setTimeout(r, 700));
      const chosen = SCENARIOS.filter((s) => s.suite === b.suite && (!b.scenario || s.id === b.scenario));
      if (!chosen.length) throw new HttpError(404, `no scenario ${b.scenario ?? ""} in suite ${b.suite}`);
      const results = chosen.map((s, i) => {
        const fails = b.mode === "replay" && s.id === "c3_relative_eta";
        return {
          scenario_id: s.id,
          passed: !fails,
          pass_k: fails ? 0 : 1,
          trials: b.k,
          cost_usd: b.mode === "scripted" ? 0 : 0.0011 + (i % 5) * 0.0002,
          failures: fails
            ? [{ step_index: 1, check: "expect.actions[0]", message: "expected a tms_note containing '07:45', found 'ETA 7:15 PM'" }]
            : [],
        };
      });
      return { suite: b.suite, mode: b.mode, k: b.k, scenarios: results };
    }
    case "GET /health":
      return health();
    default:
      throw new HttpError(404, `no mock route for ${method} ${path}`);
  }
}

export async function mockFetch(input: string, init?: RequestInit): Promise<Response> {
  const url = new URL(input, "http://mock.local");
  const path = url.pathname.replace(/^\/api/, "");
  const method = (init?.method ?? "GET").toUpperCase();
  const body = typeof init?.body === "string" && init.body ? JSON.parse(init.body) : undefined;
  await new Promise((r) => setTimeout(r, 40));
  try {
    const data = await route(method, path, url.searchParams, body);
    return new Response(JSON.stringify(data), { status: method === "POST" && path === "/loads" ? 201 : 200, headers: { "Content-Type": "application/json" } });
  } catch (error) {
    const status = error instanceof HttpError ? error.status : 500;
    return new Response(JSON.stringify({ detail: error instanceof Error ? error.message : String(error) }), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }
}
