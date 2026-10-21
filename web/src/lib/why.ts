/**
 * "Why did the agent do this?": one run in plain words. What it understood (the event,
 * the intent, the gate's call), what it followed (the SOP sections, the facility notes),
 * what it did (each action, accepted or refused with the rule's reason), what it left
 * open, and its own one-line answer.
 */

import type { Load, LoadEvent, RecordedStep, RunRecord, TrajectoryStep, Violation } from "../api";
import { clip } from "./trajectory";
import { describeEvent, recipient } from "./conversation";
import { humanize, phrase, stageLabel } from "./load";

export interface SectionInfo {
  title: string;
  sources: string[];
}

export interface WhyLine {
  text: string;
  ok: boolean;
  /** Why a rule refused it. */
  reason: string | null;
}

export interface WhySummary {
  /** "The agent acted", "The agent did nothing". */
  headline: string;
  understood: {
    event: string;
    intent: string | null;
    /** "Needs the agent: the driver asks for the PO." */
    gate: string | null;
    /** P(act now) when the gate engine gives one, 0 to 1. */
    actProbability: number | null;
    /** The probability of the intent it read, when the gate engine gives one. */
    intentProbability: number | null;
  };
  followed: { id: string; title: string; layer: string }[];
  notes: string[];
  did: WhyLine[];
  /** Null when the ledger after the run is not known. */
  leftOpen: string[] | null;
  rationale: string | null;
}

const HEADLINE: Record<RunRecord["status"], string> = {
  acted: "The agent acted",
  escalated: "The agent handed it to a Hero",
  noop: "The agent did nothing",
  skipped: "Nothing to do",
  failed: "The run failed",
};

/** Which layer of the SOP a section came from: the client's own, a load type's, or the standard. */
export function layerOf(sources: readonly string[]): string {
  if (sources.some((s) => s.includes("/types/"))) return "load type";
  if (sources.some((s) => s.includes("clients/"))) return sources.some((s) => s.includes("standard/")) ? "standard, extended by the client" : "client";
  return "standard";
}

/** Ids written into a sentence read as words: "eta_request follow-up timer" reads "ETA request follow-up timer". */
export function plainIds(text: string): string {
  return text.replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, (id) => phrase(id));
}

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));
const lower = (s: string): string => s.charAt(0).toLowerCase() + s.slice(1);

/** One action in plain words: "Text the driver: “The PO number is 55821.”". */
export function describeAction(type: string, args: Record<string, unknown>, load: Pick<Load, "contacts"> | undefined): string {
  const said = (v: unknown) => (str(v) ? `: “${clip(str(v), 90)}”` : "");
  const kind = (v: unknown) => phrase(str(v) || "request");
  switch (type) {
    case "send_sms":
      return `Text ${recipient(load, str(args.contact_id))}${str(args.purpose) === "eta_request" ? " for the ETA" : ""}${said(args.text)}`;
    case "send_email": {
      const ids = Array.isArray(args.to_contact_ids) ? args.to_contact_ids.map(str) : [];
      const to = ids.map((id) => recipient(load, id)).join(" and ") || "someone";
      return `Email ${to}${said(args.subject || args.body)}`;
    }
    case "post_broker_slack":
      return `Message the broker on Slack${said(args.text)}`;
    case "post_internal_slack":
      return `Post in the team's Slack${said(args.text)}`;
    case "write_tms_note":
      return `Write a TMS note${said(args.text)}`;
    case "create_hero_task":
      return `Create ${args.urgent ? "an urgent" : "a"} Hero task${said(args.title)}`;
    case "update_stage":
      return `Move the load to ${stageLabel(str(args.stage))}`;
    case "schedule_timer":
      return `Follow up on the ${kind(args.kind)} in ${str(args.due_in_minutes)} min`;
    case "defer_obligation":
      return `Wait ${str(args.minutes)} min more for the ${kind(args.kind)}`;
    case "close_obligation":
      return `Close the ${kind(args.kind)} as ${str(args.status)}`;
    case "open_obligation":
      return `Open a ${kind(args.kind)}`;
    default:
      return humanize(type);
  }
}

/** The rule's message of a refused call: "rejected by attempt_cap: 2 of 2 used" reads "Attempt cap: 2 of 2 used". */
export function refusalReason(step: Pick<TrajectoryStep, "result">, violation?: Violation): string {
  if (violation) return `${humanize(violation.rule)}: ${violation.message}`;
  const match = /rejected by ([a-z][a-z0-9_]*):?\s*(.*)/is.exec(step.result);
  return match ? `${humanize(match[1]!)}${match[2] ? `: ${match[2].trim()}` : ""}` : step.result;
}

function actions(run: RunRecord, load: Pick<Load, "contacts"> | undefined): WhyLine[] {
  const calls = (run.trajectory ?? []).filter((s) => s.accepted !== null);
  if (calls.length) {
    return calls.map((s) => ({
      text: describeAction(s.tool, s.args, load),
      ok: s.accepted === true,
      reason: s.accepted ? null : refusalReason(s),
    }));
  }
  // A record without a trajectory: the plan, with the validator's verdicts by index.
  return (run.plan?.actions ?? []).map((action, index) => {
    const violation = run.violations.find((v) => v.action_index === index);
    const { type, ...args } = action as { type: string } & Record<string, unknown>;
    return { text: describeAction(type, args, load), ok: !violation, reason: violation ? refusalReason({ result: "" }, violation) : null };
  });
}

/** What the run left open: each open request with its attempts, and the next follow-up. */
export function leftOpen(step: Pick<RecordedStep, "obligations" | "timers">): string[] {
  const lines: string[] = [];
  for (const [kind, o] of Object.entries(step.obligations)) {
    if (o.status !== "open") continue;
    const cap = o.max_attempts ? ` of ${o.max_attempts}` : "";
    lines.push(`${humanize(kind)} open, ${o.attempts}${cap} attempt${(o.max_attempts ?? o.attempts) === 1 ? "" : "s"}`);
  }
  if (step.timers.due_at) {
    const more = step.timers.pending > 1 ? ` (${step.timers.pending} pending)` : "";
    lines.push(`Next follow-up at ${step.timers.due_at}${more}`);
  }
  return lines.length ? lines : ["Nothing open"];
}

export function explainRun(
  run: RunRecord,
  context: {
    event: LoadEvent | null;
    load: Pick<Load, "contacts"> | undefined;
    sections: ReadonlyMap<string, SectionInfo>;
    /** The recorded step the run belongs to, for the ledger after it. */
    step: Pick<RecordedStep, "obligations" | "timers"> | null;
  },
): WhySummary {
  const intent = run.intent ?? run.gate?.intent ?? null;
  // A decision model's reason only restates its probabilities, which are shown as numbers.
  const scores = run.gate_scores ?? null;
  const reason = run.gate?.reason && !scores ? `: ${lower(plainIds(run.gate.reason))}` : "";
  const gate = run.gate ? `${run.gate.act ? "Needs the agent" : "Needs no action"}${reason}` : null;
  const act = scores?.act;
  const intentScore = intent && scores ? scores.intents[intent] : undefined;
  return {
    headline: HEADLINE[run.status] ?? humanize(run.status),
    understood: {
      event: describeEvent(context.event, context.load),
      intent: intent ? phrase(intent) : null,
      gate,
      actProbability: typeof act === "number" ? act : null,
      intentProbability: typeof intentScore === "number" ? intentScore : null,
    },
    followed: run.sop_section_ids.map((id) => {
      const section = context.sections.get(id);
      return { id, title: section?.title ?? humanize(id), layer: section ? layerOf(section.sources) : "" };
    }),
    notes: (run.facility_facts ?? []).map((f) => `${f.facility_name}: ${f.text}`),
    did: actions(run, context.load),
    leftOpen: context.step ? leftOpen(context.step) : null,
    rationale: run.plan?.rationale?.trim() || (run.status === "skipped" ? run.skip_reason : null) || (run.status === "failed" ? run.error : null),
  };
}
