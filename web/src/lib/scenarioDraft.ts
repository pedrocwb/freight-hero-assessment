/**
 * "Save this conversation as a scenario": the steps the API recorded for a load
 * (`GET /api/loads/{id}/recording`), each with expectations drafted from what actually
 * happened (what was sent, to whom and how many, the requests and their attempts, the
 * pending timers, the run's status, the model calls). The person unchecks what should
 * not be held fixed, adds `forbid` lines and edits the notes; `toDraft` makes the body of
 * `POST /api/scenarios`, in the shapes of `evalkit/spec.py`.
 */

import type { Load, OutboxItem, RecordedStep, RunRecord, RunStatus, ScenarioDraft } from "../api";
import { recipient } from "./conversation";
import { humanize, roleLabel } from "./load";
import { clockTime } from "./time";

export type ExpectField = "actions" | "no_actions" | "obligations" | "timers" | "run_status" | "llm_calls";

export interface Expectation {
  id: string;
  field: ExpectField;
  label: string;
  /** What goes into `expect[field]` (an action matcher, one obligation, ...). */
  value: unknown;
  on: boolean;
}

export const FORBIDS = {
  sms: { label: "No SMS", matcher: { kind: "sms" } },
  email: { label: "No email", matcher: { kind: "email" } },
  hero_task: { label: "No Hero task", matcher: { kind: "hero_task" } },
  eta_request: { label: "No ETA request", matcher: { kind: "sms", purpose: "eta_request" } },
} as const;
export type ForbidKey = keyof typeof FORBIDS;
export const FORBID_KEYS = Object.keys(FORBIDS) as ForbidKey[];

export interface DraftStep {
  index: number;
  /** "07:12", the load's local time. */
  at: string;
  kind: RecordedStep["kind"];
  /** What the step injected, in a line. */
  summary: string;
  note: string;
  expectations: Expectation[];
  forbid: ForbidKey[];
}

const KIND_LABEL: Record<string, [string, string]> = {
  sms: ["SMS", "SMS"],
  email: ["email", "emails"],
  broker_slack: ["broker Slack message", "broker Slack messages"],
  hero_task: ["Hero task", "Hero tasks"],
  tms_note: ["TMS note", "TMS notes"],
  internal_slack: ["team Slack post", "team Slack posts"],
  stage_update: ["stage update", "stage updates"],
};

const STATUS_LABEL: Record<RunStatus, string> = {
  acted: "acted",
  escalated: "handed it to a Hero",
  noop: "did nothing",
  skipped: "was skipped",
  failed: "failed",
};

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/** The step's injection in a line: "Driver by SMS: “What's the PO number?”". */
export function stepSummary(step: RecordedStep, load: Pick<Load, "contacts"> | undefined): string {
  if (step.message) {
    const role = load?.contacts.find((c) => c.id === step.message!.sender)?.role;
    const from = step.message.sender ? roleLabel(role ?? null) : "Unknown sender";
    const files = step.message.attachments.map((a) => a.filename);
    const body = step.message.body.trim() ? `: “${step.message.body.trim()}”` : "";
    return `${from} by ${step.message.channel === "sms" ? "SMS" : humanize(step.message.channel)}${files.length ? ` with ${files.join(", ")}` : ""}${body}`;
  }
  if (step.ping) return `Location ping, ${step.ping.delay_risk} delay risk`;
  return "Skip to the next timer";
}

/** The recipient a matcher can name: one contact id (SMS, a one-person email), else none. */
function matcherTarget(item: Pick<OutboxItem, "kind" | "payload">): string | null {
  if (item.kind === "sms") return str(item.payload.contact_id) || null;
  if (item.kind === "email") {
    const ids = Array.isArray(item.payload.to_contact_ids) ? item.payload.to_contact_ids.map(str) : [];
    return ids.length === 1 ? ids[0]! : null;
  }
  return null;
}

/** The expectations of a step, drafted from what it changed. */
export function draftExpectations(
  step: RecordedStep,
  outbox: readonly OutboxItem[],
  load: Pick<Load, "contacts"> | undefined,
): Expectation[] {
  const made = step.outbox_ids.map((id) => outbox.find((o) => o.outbox_id === id)).filter((o): o is OutboxItem => Boolean(o));
  const out: Expectation[] = [];
  const groups = new Map<string, { kind: string; to: string | null; count: number }>();
  for (const item of made) {
    const to = matcherTarget(item);
    const key = `${item.kind}|${to ?? ""}`;
    const group = groups.get(key) ?? { kind: item.kind, to, count: 0 };
    group.count += 1;
    groups.set(key, group);
  }
  for (const [key, g] of groups) {
    const [one, many] = KIND_LABEL[g.kind] ?? [humanize(g.kind), humanize(g.kind)];
    const label = `${g.count} ${g.count === 1 ? one : many}${g.to ? ` to ${recipient(load, g.to)}` : ""}`;
    out.push({ id: `actions:${key}`, field: "actions", label, value: { kind: g.kind, ...(g.to ? { to: g.to } : {}), count: g.count }, on: true });
  }
  if (!made.length) out.push({ id: "no_actions", field: "no_actions", label: "Nothing sent", value: true, on: true });
  for (const [kind, o] of Object.entries(step.obligations)) {
    const label = `${humanize(kind)} ${o.status}, ${o.attempts} attempt${o.attempts === 1 ? "" : "s"}`;
    out.push({ id: `obligations:${kind}`, field: "obligations", label, value: { [kind]: { status: o.status, attempts: o.attempts } }, on: true });
  }
  const timers = step.timers;
  out.push({
    id: "timers",
    field: "timers",
    label: timers.pending ? `${timers.pending} timer${timers.pending === 1 ? "" : "s"} pending, next at ${timers.due_at}` : "No timer pending",
    value: timers.due_at ? { pending: timers.pending, due_at: timers.due_at } : { pending: timers.pending },
    on: true,
  });
  if (step.run_statuses.length) {
    const label = step.run_statuses.length === 1
      ? `The agent ${STATUS_LABEL[step.run_statuses[0]!]}`
      : `Runs: ${step.run_statuses.map((s) => STATUS_LABEL[s]).join(", then ")}`;
    out.push({ id: "run_status", field: "run_status", label, value: step.run_statuses, on: true });
  } else {
    out.push({ id: "run_status", field: "run_status", label: "No run", value: [], on: false });
  }
  if (step.llm_calls !== null) {
    out.push({ id: "llm_calls", field: "llm_calls", label: `${step.llm_calls} model call${step.llm_calls === 1 ? "" : "s"}`, value: step.llm_calls, on: true });
  }
  return out;
}

export function draftSteps(
  recording: readonly RecordedStep[],
  outbox: readonly OutboxItem[],
  load: Pick<Load, "contacts"> | undefined,
  timezone: string,
): DraftStep[] {
  return recording.map((step) => {
    const summary = stepSummary(step, load);
    return {
      index: step.index,
      at: clockTime(step.at, timezone),
      kind: step.kind,
      summary,
      note: summary,
      expectations: draftExpectations(step, outbox, load),
      forbid: [],
    };
  });
}

/** `expect` and `forbid` of one step, from its checked expectations. */
export function stepBody(step: DraftStep): { expect: Record<string, unknown>; forbid: Record<string, unknown>[] } {
  const expect: Record<string, unknown> = {};
  for (const e of step.expectations) {
    if (!e.on) continue;
    if (e.field === "actions") expect.actions = [...((expect.actions as unknown[]) ?? []), e.value];
    else if (e.field === "obligations") expect.obligations = { ...((expect.obligations as object) ?? {}), ...(e.value as object) };
    else expect[e.field] = e.value;
  }
  return { expect, forbid: step.forbid.map((key) => ({ ...FORBIDS[key].matcher })) };
}

/** The SOP sections the conversation's runs used, as the `sop:<id>` tags the coverage lint reads. */
export function suggestTags(runs: readonly Pick<RunRecord, "run_id" | "sop_section_ids">[], recording: readonly RecordedStep[]): string[] {
  const ids = new Set(recording.flatMap((s) => s.run_ids));
  const sections = runs.filter((r) => ids.has(r.run_id)).flatMap((r) => r.sop_section_ids);
  return ["regression", ...[...new Set(sections)].map((id) => `sop:${id}`)];
}

/** A scenario id that names the load and the moment: "recorded_481207_0731". */
export function suggestId(loadId: string, now: string, timezone: string): string {
  const slug = loadId.toLowerCase().replace(/[^a-z0-9]+/g, "_");
  return `recorded_${slug}_${clockTime(now, timezone).replace(":", "")}`;
}

export const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

export function toDraft(args: {
  loadId: string;
  id: string;
  title: string;
  suite: string;
  tags: string;
  description: string;
  steps: readonly DraftStep[];
}): ScenarioDraft {
  return {
    load_id: args.loadId,
    id: args.id.trim(),
    title: args.title.trim(),
    suite: args.suite.trim() || "core",
    tags: args.tags.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean),
    description: args.description.trim(),
    steps: args.steps.map((s) => ({ index: s.index, note: s.note.trim(), ...stepBody(s) })),
  };
}
