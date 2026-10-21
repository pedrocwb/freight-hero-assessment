/**
 * A load's story, assembled from its snapshot: each inbound event (a message, a ping, a
 * timer, a document) followed by the runs it caused, and for each run what it produced.
 *
 * The event log also holds the agent's own actions (`agent_action`, outbound); they are
 * left out here because the outbox already says what each run sent, with its delivery.
 */

import type { Action, EventType, Load, LoadEvent, LoadSnapshot, OutboxItem, OutboxKind, RunRecord, Violation } from "../api";
import { channelLabel, humanize, roleLabel } from "./load";

/** The event types that start a run (`domain.events.RUN_TRIGGERS`). */
const TRIGGERS: ReadonlySet<EventType> = new Set([
  "inbound_message",
  "risk_changed",
  "timer_due",
  "attachment_received",
  "hero_update",
  "document_processed",
]);

export interface TimelineRun {
  run: RunRecord;
  /** 1 for the load's first run, in start order. */
  number: number;
}

export interface TimelineEntry {
  key: string;
  /** The event's time, or the run's start when its event is not in the log. */
  at: string;
  event: LoadEvent | null;
  runs: TimelineRun[];
}

function time(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

/** The load's runs in start order, numbered from 1. */
export function numberRuns(runs: readonly RunRecord[]): TimelineRun[] {
  return runs
    .map((run, index) => ({ run, index }))
    .sort((a, b) => time(a.run.started_at) - time(b.run.started_at) || a.index - b.index)
    .map(({ run }, i) => ({ run, number: i + 1 }));
}

/**
 * Every inbound event in time order, each with the runs it caused (none while it waits in
 * the queue, or for a ping that changed no risk band). A run whose event is missing from
 * the log still shows, at its start time.
 */
export function buildTimeline(snapshot: Pick<LoadSnapshot, "events" | "runs">): TimelineEntry[] {
  const byEvent = new Map<string, TimelineRun[]>();
  for (const numbered of numberRuns(snapshot.runs)) {
    const list = byEvent.get(numbered.run.event_id);
    if (list) list.push(numbered);
    else byEvent.set(numbered.run.event_id, [numbered]);
  }

  const entries: TimelineEntry[] = [];
  for (const event of snapshot.events) {
    if (event.direction !== "inbound" || !TRIGGERS.has(event.type)) continue;
    entries.push({ key: `e:${event.event_id}`, at: event.occurred_at, event, runs: byEvent.get(event.event_id) ?? [] });
    byEvent.delete(event.event_id);
  }
  for (const [eventId, runs] of byEvent) {
    entries.push({ key: `r:${eventId}`, at: runs[0]!.run.started_at, event: null, runs });
  }
  // A stable sort: at the same instant the log's order stands.
  return entries
    .map((entry, order) => ({ entry, order }))
    .sort((a, b) => time(a.entry.at) - time(b.entry.at) || a.order - b.order)
    .map(({ entry }) => entry);
}

export type EventKind = "message" | "ping" | "timer" | "document" | "hero";

export function eventKind(event: Pick<LoadEvent, "type">): EventKind {
  switch (event.type) {
    case "risk_changed":
      return "ping";
    case "timer_due":
      return "timer";
    case "attachment_received":
    case "document_processed":
      return "document";
    case "hero_update":
      return "hero";
    default:
      return "message";
  }
}

/** One line for what woke the agent: "Driver drv_20931 by SMS", "Delay risk low to high". */
export function describeEvent(event: LoadEvent | null, load: Pick<Load, "contacts"> | undefined): string {
  if (!event) return "An event missing from the log";
  const data = event.data ?? {};
  switch (event.type) {
    case "risk_changed":
      return `Delay risk ${String(data.previous ?? "?")} to ${String(data.current ?? "?")}`;
    case "timer_due":
      return `Follow-up timer due (${humanize(String(data.kind ?? "timer"))})`;
    case "inbound_message": {
      const role = load?.contacts.find((c) => c.id === event.sender_contact_id)?.role;
      const from = event.sender_contact_id ? `${roleLabel(role)} ${event.sender_contact_id}` : "Unknown sender";
      const files = event.attachments.length ? `, with ${event.attachments.map((a) => a.filename).join(", ")}` : "";
      return `${from} by ${channelLabel(event.channel)}${files}`;
    }
    case "attachment_received":
      return `Attachment ${event.attachments.map((a) => a.filename).join(", ")}`;
    case "document_processed": {
      const type = typeof data.type_id === "string" && data.type_id ? humanize(data.type_id) : "Document";
      return `${type} read by the pipeline (${humanize(String(data.status ?? "processed")).toLowerCase()})`;
    }
    case "hero_update":
      return "Hero update";
    default:
      return humanize(event.type);
  }
}

// --- what a run produced ------------------------------------------------------------------

export type ActionChannel = "sms" | "email" | "broker_slack" | "tms_note" | "hero_task" | "other";

const CHANNEL_OF: Record<OutboxKind, ActionChannel> = {
  sms: "sms",
  email: "email",
  broker_slack: "broker_slack",
  tms_note: "tms_note",
  hero_task: "hero_task",
  internal_slack: "other",
  stage_update: "other",
};

const ACTION_CHANNELS: readonly { channel: ActionChannel; label: string }[] = [
  { channel: "sms", label: "SMS" },
  { channel: "email", label: "Email" },
  { channel: "broker_slack", label: "Broker Slack" },
  { channel: "tms_note", label: "TMS note" },
  { channel: "hero_task", label: "Hero task" },
  { channel: "other", label: "Other" },
];

export interface ActionGroup {
  channel: ActionChannel;
  label: string;
  items: OutboxItem[];
}

/** What the run put in the outbox, grouped by channel in a fixed order, oldest first. */
export function groupActions(outbox: readonly OutboxItem[], runId: string): ActionGroup[] {
  const mine = outbox
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => item.run_id === runId)
    .sort((a, b) => time(a.item.created_at) - time(b.item.created_at) || a.index - b.index)
    .map(({ item }) => item);
  return ACTION_CHANNELS.map(({ channel, label }) => ({
    channel,
    label,
    items: mine.filter((item) => (CHANNEL_OF[item.kind] ?? "other") === channel),
  })).filter((group) => group.items.length > 0);
}

const LEDGER_ACTIONS: ReadonlySet<Action["type"]> = new Set([
  "schedule_timer",
  "defer_obligation",
  "close_obligation",
  "open_obligation",
]);

export interface DroppedAction {
  /** Null when the violation names no action of the plan. */
  action: Action | null;
  violations: Violation[];
}

export interface PlanReview {
  /** The timers and obligations the run changed: the actions that reach no channel. */
  ledger: Action[];
  /** What the validator refused, with its reasons. */
  dropped: DroppedAction[];
}

export function reviewPlan(run: Pick<RunRecord, "plan" | "violations">): PlanReview {
  const actions = run.plan?.actions ?? [];
  const byIndex = new Map<number, Violation[]>();
  for (const v of run.violations) byIndex.set(v.action_index, [...(byIndex.get(v.action_index) ?? []), v]);
  const ledger = actions.filter((action, i) => LEDGER_ACTIONS.has(action.type) && !byIndex.has(i));
  const dropped = [...byIndex.entries()]
    .sort(([a], [b]) => a - b)
    .map(([index, violations]) => ({ action: actions[index] ?? null, violations }));
  return { ledger, dropped };
}
