import type { Load, LoadEvent, Obligation, OutboxItem, RunRecord, Timer } from "../api";

export interface DurableSnapshot {
  load: Load & {
    sop_snapshot?: { version: string; policy: DemoPolicy } | null;
    automation_stop_requested_at?: string | null;
    automation_stopped_at?: string | null;
  };
  events: LoadEvent[];
  obligations: Obligation[];
  outbox: (Omit<OutboxItem, "status"> & { status: OutboxItem["status"] | "cancelled"; accepted_at: string | null })[];
  timers: Timer[];
  runs: RunRecord[];
}

export interface DemoPolicy {
  max_attempts: number;
  interval_minutes: number;
  body: string;
}

export interface DurableHealth {
  model_ready: boolean;
  gateways: string;
}

export async function durableRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/durable${path}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Não foi possível consultar a operação.");
  return data as T;
}

export function progress(snapshot: Pick<DurableSnapshot, "outbox" | "obligations" | "timers" | "runs"> & { load?: DurableSnapshot["load"] }) {
  const maxAttempts = snapshot.load?.sop_snapshot?.policy.max_attempts ?? 10;
  const intervalMinutes = snapshot.load?.sop_snapshot?.policy.interval_minutes ?? 1;
  const sent = snapshot.outbox.filter((o) => o.status === "sent");
  const sms = sent.filter((o) => o.kind === "sms" && o.payload.purpose === "eta_request");
  const heroes = sent.filter((o) => o.kind === "hero_task");
  const hero = heroes[0];
  const obligation = snapshot.obligations.find((o) => o.kind === "eta_request");
  const timer = snapshot.timers.filter((t) => t.status === "pending").sort((a, b) => a.due_at.localeCompare(b.due_at))[0];
  const failed = snapshot.runs.find((r) => r.error || r.status === "failed")
    || snapshot.outbox.find((o) => o.status === "failed");
  const complete = !failed && sms.length === maxAttempts && heroes.length === 1
    && obligation?.status === "exhausted" && !timer
    && !snapshot.outbox.some((o) => o.status === "pending");
  return { sms, hero, obligation, timer, failed, complete, maxAttempts, intervalMinutes };
}

export function remaining(due: string, now: number): string {
  const seconds = Math.max(0, Math.ceil((Date.parse(due) - now) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Inbound driver messages and confirmed/staged outbound actions in one conversation. */
export function conversation(snapshot: Pick<DurableSnapshot, "outbox" | "events">) {
  return [
    ...snapshot.outbox.map((item) => ({ kind: "outbound" as const, key: item.outbox_id, at: item.accepted_at || item.created_at, item })),
    ...snapshot.events.filter((event) => event.type === "inbound_message" && event.sender_contact_id === "drv_aurora")
      .map((event) => ({ kind: "driver" as const, key: event.event_id, at: event.occurred_at, event })),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}
