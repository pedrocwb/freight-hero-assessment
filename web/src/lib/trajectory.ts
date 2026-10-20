/**
 * The agent loop's trajectory (`RunRecord.trajectory`, card AGT), made readable: one table
 * row per tool call, with its arguments on one line and what the rules said.
 */

import type { TrajectoryStep } from "../api";

/** The text cut to `max` characters, with an ellipsis when something was cut. */
export function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

function value(v: unknown, max: number): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return /[\s,=]/.test(v) || v === "" ? `"${clip(v, max)}"` : clip(v, max);
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map((item) => value(item, max)).join(", ")}]`;
  return clip(JSON.stringify(v), max);
}

/**
 * The arguments of a call on one line, `key=value` in the order the model gave them:
 * strings with spaces quoted and clipped, lists in brackets.
 */
export function compactArgs(args: Record<string, unknown> | null | undefined, max = 60): string {
  const entries = Object.entries(args ?? {});
  if (!entries.length) return "no arguments";
  return entries.map(([key, v]) => `${key}=${value(v, max)}`).join(", ");
}

export type StepVerdict = "read" | "accepted" | "rejected";

export function stepVerdict(step: Pick<TrajectoryStep, "accepted">): StepVerdict {
  if (step.accepted === true) return "accepted";
  if (step.accepted === false) return "rejected";
  return "read";
}

/** The rule a rejected call names: "rejected by recipient_is_contact: ..." gives the rule id. */
export function rejectionRule(result: string): string | null {
  const match = /rejected by ([a-z][a-z0-9_]*)/i.exec(result);
  return match ? match[1]! : null;
}

export interface TrajectoryTally {
  steps: number;
  reads: number;
  accepted: number;
  rejected: number;
  costUsd: number;
  /** Distinct tools, in the order of their first call. */
  tools: string[];
}

export function tallyTrajectory(steps: readonly TrajectoryStep[]): TrajectoryTally {
  const tally: TrajectoryTally = { steps: steps.length, reads: 0, accepted: 0, rejected: 0, costUsd: 0, tools: [] };
  for (const step of steps) {
    const verdict = stepVerdict(step);
    if (verdict === "read") tally.reads += 1;
    else if (verdict === "accepted") tally.accepted += 1;
    else tally.rejected += 1;
    tally.costUsd += step.cost_usd || 0;
    if (!tally.tools.includes(step.tool)) tally.tools.push(step.tool);
  }
  return tally;
}
