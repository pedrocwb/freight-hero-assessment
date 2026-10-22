import type { LoadEvent, OutboxItem, RunRecord } from "../domain";

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
