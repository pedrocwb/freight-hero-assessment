/**
 * A load's story as a chat: the messages the driver, dispatcher and broker sent (from the
 * event log) and the agent's own (from the outbox), in time order, with the system events
 * and the agent's quiet work (a timer fired, a Hero task, a TMS note, a run that did
 * nothing) as small markers between them.
 *
 * The event log also holds the agent's actions (`agent_action`, outbound); they are left
 * out because the outbox says the same, with the delivery status.
 */

import type { Contact, Load, LoadEvent, LoadSnapshot, OutboxItem, RunRecord } from "../api";
import { channelLabel, humanize, phrase, roleLabel, stageLabel } from "./load";

export type Side = "in" | "out";

export interface ChatMessage {
  kind: "message";
  key: string;
  at: string;
  side: Side;
  /** "Driver", "Dispatcher", "Agent". */
  from: string;
  /** For the agent's messages: "the driver", "the dispatcher and the broker". */
  to: string | null;
  channel: string;
  subject: string | null;
  body: string;
  attachments: string[];
  /** Why the agent sent it ("ETA request", "Reply"), for its own messages. */
  purpose: string | null;
  /** The run that sent it (out), or the first run it caused (in). */
  runId: string | null;
  delivery: OutboxItem["status"] | null;
}

export type MarkerTone = "system" | "agent" | "nothing" | "alert";

export interface ChatMarker {
  kind: "marker";
  key: string;
  at: string;
  tone: MarkerTone;
  text: string;
  detail: string | null;
  runId: string | null;
}

export type ChatItem = ChatMessage | ChatMarker;

const text = (value: unknown): string => (typeof value === "string" ? value : value == null ? "" : String(value));

function time(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

function contactOf(load: Pick<Load, "contacts"> | undefined, id: string | null): Contact | undefined {
  return id ? load?.contacts.find((c) => c.id === id) : undefined;
}

/** "the driver" for a load's contact, the id itself for anyone else. */
export function recipient(load: Pick<Load, "contacts"> | undefined, id: string): string {
  const contact = contactOf(load, id);
  return contact ? `the ${roleLabel(contact.role).toLowerCase()}` : id;
}

function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** One line for what woke the agent: "Driver by SMS", "Delay risk low to high". */
export function describeEvent(event: LoadEvent | null, load: Pick<Load, "contacts"> | undefined): string {
  if (!event) return "An event missing from the log";
  const data = event.data ?? {};
  switch (event.type) {
    case "risk_changed":
      return `Delay risk went from ${text(data.previous) || "?"} to ${text(data.current) || "?"}`;
    case "timer_due":
      return `The follow-up timer for the ${phrase(text(data.kind) || "request")} fired`;
    case "inbound_message": {
      const role = contactOf(load, event.sender_contact_id)?.role;
      const from = event.sender_contact_id ? roleLabel(role) : "An unknown sender";
      const files = event.attachments.length ? ` with ${event.attachments.map((a) => a.filename).join(", ")}` : "";
      const said = event.body.trim() ? `: “${event.body.trim()}”` : "";
      return `${from} wrote by ${channelLabel(event.channel)}${files}${said}`;
    }
    case "attachment_received":
      return `A file arrived: ${event.attachments.map((a) => a.filename).join(", ")}`;
    case "document_processed": {
      const type = text(data.type_id) ? humanize(text(data.type_id)) : "A document";
      return `${type} was read (${humanize(text(data.status) || "processed").toLowerCase()})`;
    }
    case "hero_update":
      return event.body.trim() ? `A Hero wrote: “${event.body.trim()}”` : "A Hero updated the load";
    default:
      return humanize(event.type);
  }
}

function eventMarker(event: LoadEvent, runId: string | null): ChatMarker | null {
  const base = { kind: "marker" as const, key: `e:${event.event_id}`, at: event.occurred_at, runId, detail: null };
  const data = event.data ?? {};
  switch (event.type) {
    case "risk_changed": {
      const current = text(data.current);
      return { ...base, tone: current === "high" ? "alert" : "system", text: `Delay risk ${text(data.previous) || "?"} → ${current || "?"}` };
    }
    case "timer_due":
      return { ...base, tone: "system", text: `Follow-up timer fired (${humanize(text(data.kind) || "timer")})` };
    case "attachment_received":
      return { ...base, tone: "system", text: `File received: ${event.attachments.map((a) => a.filename).join(", ")}` };
    case "document_processed": {
      const type = text(data.type_id) ? humanize(text(data.type_id)) : "Document";
      const status = text(data.status);
      return {
        ...base,
        tone: status === "needs_review" || status === "failed" ? "alert" : "system",
        text: `${type} read: ${humanize(status || "processed").toLowerCase()}`,
      };
    }
    case "hero_update":
      return { ...base, tone: "system", text: "Hero update", detail: event.body.trim() || null };
    default:
      return null;
  }
}

const PURPOSE_LABEL: Record<string, string> = {
  eta_request: "ETA request",
  reply: "Reply",
  escalation: "Escalation",
  info: "Update",
};

function outboxItem(item: OutboxItem, load: Pick<Load, "contacts"> | undefined): ChatItem {
  const p = item.payload ?? {};
  const message = {
    kind: "message" as const,
    key: `o:${item.outbox_id}`,
    at: item.created_at,
    side: "out" as const,
    from: "Agent",
    subject: null,
    attachments: [],
    runId: item.run_id,
    delivery: item.status,
    purpose: text(p.purpose) ? PURPOSE_LABEL[text(p.purpose)] ?? humanize(text(p.purpose)) : null,
  };
  const marker = { kind: "marker" as const, key: `o:${item.outbox_id}`, at: item.created_at, runId: item.run_id };
  switch (item.kind) {
    case "sms":
      return { ...message, to: recipient(load, text(p.contact_id)), channel: "SMS", body: text(p.text) };
    case "email": {
      const ids = Array.isArray(p.to_contact_ids) ? p.to_contact_ids.map(text) : [];
      return { ...message, to: joinNames(ids.map((id) => recipient(load, id))), channel: "Email", subject: text(p.subject) || null, body: text(p.body) };
    }
    case "broker_slack":
      return { ...message, to: "the broker", channel: "Broker Slack", body: text(p.text) };
    case "hero_task":
      return {
        ...marker,
        tone: "alert",
        text: `${p.urgent ? "Urgent Hero task" : "Hero task"}: ${text(p.title)}`,
        detail: text(p.description) || null,
      };
    case "tms_note":
      return { ...marker, tone: "agent", text: "TMS note", detail: text(p.text) || null };
    case "internal_slack":
      return { ...marker, tone: "agent", text: "Posted in the team's Slack", detail: text(p.text) || null };
    case "stage_update":
      return { ...marker, tone: "agent", text: `Stage → ${stageLabel(text(p.stage))}`, detail: null };
    default:
      return { ...marker, tone: "agent", text: humanize(text(item.kind)), detail: text(p.text) || null };
  }
}

/** The marker of a run that sent nothing: it did nothing, was skipped, failed, or only changed its follow-ups. */
export function quietRunMarker(run: RunRecord): ChatMarker {
  const base = { kind: "marker" as const, key: `r:${run.run_id}`, at: run.finished_at ?? run.started_at, runId: run.run_id };
  switch (run.status) {
    case "noop":
      return { ...base, tone: "nothing", text: "The agent did nothing", detail: run.plan?.rationale || run.gate?.reason || null };
    case "skipped":
      return { ...base, tone: "nothing", text: "Nothing to do", detail: run.skip_reason ? humanize(run.skip_reason) : null };
    case "failed":
      return { ...base, tone: "alert", text: "The run failed", detail: run.error };
    default:
      return { ...base, tone: "agent", text: "The agent updated its follow-ups", detail: run.plan?.rationale || null };
  }
}

/**
 * The chat of a load, oldest first. At one instant the event log's order stands: the ETA
 * request a ping caused comes before the driver's message logged right after it, and an
 * inbound message before the replies of the run it caused.
 */
export function buildConversation(
  snapshot: Pick<LoadSnapshot, "events" | "outbox" | "runs">,
  load: Pick<Load, "contacts"> | undefined,
): ChatItem[] {
  const runOfEvent = new Map<string, string>();
  for (const run of [...snapshot.runs].sort((a, b) => time(a.started_at) - time(b.started_at))) {
    if (!runOfEvent.has(run.event_id)) runOfEvent.set(run.event_id, run.run_id);
  }
  // Where each thing sits in the log: an event at its index, the agent's output at its
  // `agent_action` entry, else just after the event that started its run.
  const position = new Map<string, number>();
  snapshot.events.forEach((event, index) => {
    position.set(`e:${event.event_id}`, index);
    const outboxId = event.data?.outbox_id;
    if (event.type === "agent_action" && typeof outboxId === "string") position.set(`o:${outboxId}`, index);
  });
  const runPosition = new Map(snapshot.runs.map((r) => [r.run_id, (position.get(`e:${r.event_id}`) ?? snapshot.events.length) + 0.5]));

  const items: ChatItem[] = [];
  for (const event of snapshot.events) {
    if (event.direction !== "inbound") continue;
    const runId = runOfEvent.get(event.event_id) ?? null;
    if (event.type === "inbound_message") {
      const contact = contactOf(load, event.sender_contact_id);
      items.push({
        kind: "message",
        key: `e:${event.event_id}`,
        at: event.occurred_at,
        side: "in",
        from: event.sender_contact_id ? roleLabel(contact?.role ?? null) : "Unknown sender",
        to: null,
        channel: channelLabel(event.channel).replace(/^email$/, "Email"),
        subject: null,
        body: event.body,
        attachments: event.attachments.map((a) => a.filename),
        purpose: null,
        runId,
        delivery: null,
      });
      continue;
    }
    const marker = eventMarker(event, runId);
    if (marker) items.push(marker);
  }
  const loud = new Set(snapshot.outbox.map((o) => o.run_id));
  for (const item of snapshot.outbox) {
    const chat = outboxItem(item, load);
    if (!position.has(chat.key)) position.set(chat.key, runPosition.get(item.run_id) ?? Infinity);
    items.push(chat);
  }
  for (const run of snapshot.runs) {
    if (loud.has(run.run_id)) continue;
    const marker = quietRunMarker(run);
    position.set(marker.key, runPosition.get(run.run_id) ?? Infinity);
    items.push(marker);
  }

  return items
    .map((item, order) => ({ item, order, seq: position.get(item.key) ?? Infinity }))
    .sort((a, b) => time(a.item.at) - time(b.item.at) || a.seq - b.seq || a.order - b.order)
    .map(({ item }) => item);
}
