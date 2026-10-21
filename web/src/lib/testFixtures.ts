/**
 * Typed fixtures for the unit tests, shaped by the API contract: the assignment's load
 * 481207 after scenario A's first three steps, the run graph as LangGraph draws it, and
 * a scenario with its run. Only the tests import this file.
 */

import type {
  LoadEvent,
  LoadSnapshot,
  OutboxItem,
  RunGraph,
  RunRecord,
  Scenario,
  ScenarioRun,
  TrajectoryStep,
} from "../api";

const LOAD_ID = "481207";

export function event(over: Partial<LoadEvent> & Pick<LoadEvent, "event_id" | "type" | "occurred_at">): LoadEvent {
  return {
    load_id: LOAD_ID,
    channel: "system",
    direction: "inbound",
    sender_contact_id: null,
    body: "",
    attachments: [],
    data: {},
    ...over,
  };
}

function step(tool: string, args: Record<string, unknown>, accepted: boolean | null, result = "ok"): TrajectoryStep {
  return { tool, args, result, accepted, cost_usd: 0.0002 };
}

export function run(over: Partial<RunRecord> & Pick<RunRecord, "run_id" | "event_id" | "started_at">): RunRecord {
  return {
    load_id: LOAD_ID,
    finished_at: over.started_at,
    status: "acted",
    gate: null,
    sop_section_ids: [],
    plan: null,
    violations: [],
    usage: [],
    skip_reason: null,
    error: null,
    trace_url: null,
    intent: null,
    broker_id: "northline",
    trajectory: [],
    sections_searched: [],
    rejections: 0,
    nodes: ["prefilter", "gate", "knowledge", "agent", "validate"],
    ...over,
  };
}

export function outbox(over: Partial<OutboxItem> & Pick<OutboxItem, "outbox_id" | "run_id" | "kind" | "created_at">): OutboxItem {
  return {
    idempotency_key: over.outbox_id,
    load_id: LOAD_ID,
    payload: {},
    status: "sent",
    attempts: 1,
    last_error: null,
    ...over,
  };
}

const PING = "risk:22776b45";
const QUESTION = "7a579362";
const TIMER = "timer:timer_04352a60afe9";

export const snapshotA: Pick<LoadSnapshot, "events" | "runs" | "outbox"> = {
  events: [
    event({ event_id: PING, type: "risk_changed", occurred_at: "2026-10-13T07:00:00-07:00", data: { previous: "low", current: "high" } }),
    event({
      event_id: `act:${PING}`,
      type: "agent_action",
      occurred_at: "2026-10-13T07:01:00-07:00",
      direction: "outbound",
      channel: "sms",
      body: "What time will you arrive at the pickup?",
    }),
    event({
      event_id: QUESTION,
      type: "inbound_message",
      occurred_at: "2026-10-13T07:12:00-07:00",
      channel: "sms",
      sender_contact_id: "drv_20931",
      body: "What's the PO number?",
    }),
    event({
      event_id: TIMER,
      type: "timer_due",
      occurred_at: "2026-10-13T07:31:00-07:00",
      data: { timer_id: "timer_04352a60afe9", kind: "eta_request" },
    }),
  ],
  // Out of start order on purpose: the timeline numbers runs by their start.
  runs: [
    run({
      run_id: "run_3",
      event_id: TIMER,
      started_at: "2026-10-13T07:31:00-07:00",
      intent: "eta_follow_up_due",
      plan: {
        rationale: "Second ETA request after one unanswered.",
        actions: [
          { type: "send_sms", contact_id: "drv_20931", text: "What time will you arrive at the pickup?", purpose: "eta_request" },
          { type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 },
        ],
      },
    }),
    run({
      run_id: "run_1",
      event_id: PING,
      started_at: "2026-10-13T07:01:00-07:00",
      intent: "delay_risk_high",
      nodes: ["prefilter", "gate", "knowledge", "agent", "tools", "agent", "tools", "agent", "validate"],
      trajectory: [
        step("send_sms", { contact_id: "drv_20931", purpose: "eta_request", text: "What time will you arrive at the pickup?" }, true, "accepted as action #1"),
        step("schedule_timer", { kind: "eta_request", due_in_minutes: 30 }, true, "accepted as action #2"),
      ],
      plan: {
        rationale: "ETA request opened, driver texted, follow-up in 30 minutes.",
        actions: [
          { type: "send_sms", contact_id: "drv_20931", text: "What time will you arrive at the pickup?", purpose: "eta_request" },
          { type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 },
        ],
      },
      trace_url: "https://smith.langchain.com/o/org/projects/p/proj/r/run_1",
    }),
    run({
      run_id: "run_2",
      event_id: QUESTION,
      started_at: "2026-10-13T07:13:00-07:00",
      intent: "question",
      plan: {
        rationale: "The driver asked for the PO, which is in the load's instructions.",
        actions: [
          { type: "send_sms", contact_id: "drv_20931", text: "The PO number is 55821.", purpose: "reply" },
          { type: "send_sms", contact_id: "drv_20931", text: "What time will you arrive?", purpose: "eta_request" },
        ],
      },
      violations: [{ rule: "one_eta_request_open", action_index: 1, message: "an ETA request is already open", severity: "low" }],
    }),
  ],
  outbox: [
    outbox({ outbox_id: "out_2", run_id: "run_2", kind: "sms", created_at: "2026-10-13T07:13:00-07:00", payload: { contact_id: "drv_20931", text: "The PO number is 55821.", purpose: "reply" } }),
    outbox({ outbox_id: "out_1", run_id: "run_1", kind: "sms", created_at: "2026-10-13T07:01:00-07:00", payload: { contact_id: "drv_20931", text: "What time will you arrive at the pickup?", purpose: "eta_request" } }),
    outbox({ outbox_id: "out_3", run_id: "run_3", kind: "sms", created_at: "2026-10-13T07:31:00-07:00", payload: { contact_id: "drv_20931", text: "What time will you arrive at the pickup?", purpose: "eta_request" } }),
  ],
};

/** `get_graph().draw_mermaid()` of a graph with an agent and tools loop. */
export const GRAPH: RunGraph = {
  mermaid: [
    "---",
    "config:",
    "  flowchart:",
    "    curve: linear",
    "---",
    "graph TD;",
    "\t__start__([<p>__start__</p>]):::first",
    "\tprefilter(prefilter)",
    "\tgate(gate)",
    "\tknowledge(knowledge)",
    "\tagent(agent)",
    "\ttools(tools)",
    "\tvalidate(validate)",
    "\t__end__([<p>__end__</p>]):::last",
    "\t__start__ --> prefilter;",
    "\tagent -.-> tools;",
    "\tagent -.-> validate;",
    "\tgate -.-> __end__;",
    "\tgate -.-> knowledge;",
    "\tknowledge --> agent;",
    "\tprefilter -.-> __end__;",
    "\tprefilter -.-> gate;",
    "\ttools --> agent;",
    "\tvalidate --> __end__;",
    "\tclassDef default fill:#f2f0ff,line-height:1.2",
    "\tclassDef first fill-opacity:0",
    "\tclassDef last fill:#bfb6fc",
    "",
  ].join("\n"),
  nodes: ["__start__", "prefilter", "gate", "knowledge", "agent", "tools", "validate", "__end__"],
};

export const SCENARIO: Scenario = {
  id: "a_client_a_high_risk",
  title: "A: Client A, delay risk goes high, the driver asks for the PO and never gives an ETA",
  suite: "core",
  tags: ["regression", "sop:delay_risk_high", "sop:driver_questions"],
  description: "Two ETA requests 30 minutes apart, a question answered from the load, then an email and a Hero task.",
  steps: [
    { index: 0, at: "07:00", note: "Ping, delay risk low to high", kind: "ping" },
    { index: 1, at: "07:12", note: "The driver asks for the PO number", kind: "message" },
    { index: 2, at: "07:31", note: "The first follow-up timer fires", kind: "wait" },
    { index: 3, at: "08:01", note: "The second follow-up timer fires", kind: "wait" },
  ],
};

export function scenarioRun(over: Partial<ScenarioRun> = {}): ScenarioRun {
  return {
    scenario_id: SCENARIO.id,
    load_id: LOAD_ID,
    status: "running",
    error: null,
    cost_usd: 0.0021,
    started_at: "2026-10-20T18:00:00Z",
    finished_at: null,
    steps: [
      { index: 0, at: "07:00", note: "Ping, delay risk low to high", status: "passed", checks: [{ name: "sms_sent", passed: true, detail: null }], run_ids: ["run_1"] },
      {
        index: 1,
        at: "07:12",
        note: "The driver asks for the PO number",
        status: "failed",
        checks: [
          { name: "reply_contains_po", passed: true, detail: null },
          { name: "no_eta_request", passed: false, detail: "an SMS with purpose eta_request was sent" },
        ],
        run_ids: ["run_2"],
      },
      { index: 2, at: "07:31", note: "The first follow-up timer fires", status: "running", checks: [], run_ids: [] },
    ],
    ...over,
  };
}
