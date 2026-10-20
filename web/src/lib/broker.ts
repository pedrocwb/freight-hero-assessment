/**
 * Editing a broker's catalog on the page (card BRK owns the real validation). The checks
 * here only catch, before a save, what the API would refuse anyway, with the same words a
 * person can act on.
 */

import type { Broker, DocumentRequirement, IntentDef, ObligationDef, Rollout } from "../api";

/** Ids of intents, obligation kinds and brokers: snake_case, starting with a letter. */
export const ID_PATTERN = /^[a-z][a-z0-9_]*$/;

export function isSnakeId(id: string): boolean {
  return ID_PATTERN.test(id);
}

/** The intent a follow-up timer of this kind wakes the agent with. */
export function followUpIntentOf(def: Pick<ObligationDef, "kind" | "follow_up_intent">): string {
  return def.follow_up_intent.trim() || `${def.kind}_follow_up_due`;
}

export function replaceAt<T>(list: readonly T[], index: number, patch: Partial<T>): T[] {
  return list.map((item, i) => (i === index ? { ...item, ...patch } : item));
}

export function removeAt<T>(list: readonly T[], index: number): T[] {
  return list.filter((_, i) => i !== index);
}

/** One example per line in the editor; blank lines dropped. */
export function linesToList(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export function newIntent(intentId: string): IntentDef {
  return {
    intent_id: intentId,
    title: intentId.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    description: "",
    examples: [],
    priority: 100,
    must_act: true,
    noop_eligible: false,
    system: false,
  };
}

export function newObligation(kind: string): ObligationDef {
  return {
    kind,
    title: kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    description: "",
    follow_up_minutes: 30,
    max_attempts: 2,
    follow_up_intent: "",
    on_exhausted: ["hero_task"],
  };
}

export function newRequirement(broker: Pick<Broker, "document_types">): DocumentRequirement {
  return {
    type_id: broker.document_types[0]?.type_id ?? "pod",
    at_stage: "at_delivery",
    within_minutes: null,
    opens_obligation: null,
  };
}

/** Structural equality of two JSON values, for the "unsaved changes" state. */
export function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function duplicates(ids: readonly string[]): string[] {
  const seen = new Set<string>();
  const twice = new Set<string>();
  for (const id of ids) (seen.has(id) ? twice : seen).add(id);
  return [...twice];
}

/** What the API would refuse in this catalog, in plain words; empty when it can be saved. */
export function brokerProblems(broker: Broker): string[] {
  const problems: string[] = [];
  if (!broker.name.trim()) problems.push("The broker needs a name.");
  const intentIds = broker.intents.map((i) => i.intent_id);
  for (const id of intentIds) {
    if (!isSnakeId(id)) problems.push(`Intent id "${id}" must be snake_case, like detention_request.`);
  }
  for (const id of duplicates(intentIds)) problems.push(`Intent ${id} is listed twice.`);
  const kinds = broker.obligations.map((o) => o.kind);
  for (const kind of kinds) {
    if (!isSnakeId(kind)) problems.push(`Obligation kind "${kind}" must be snake_case, like pod.`);
  }
  for (const kind of duplicates(kinds)) problems.push(`Obligation kind ${kind} is listed twice.`);
  for (const o of broker.obligations) {
    if (o.max_attempts < 1) problems.push(`${o.kind}: max attempts must be at least 1.`);
    if (o.follow_up_minutes < 1) problems.push(`${o.kind}: the follow-up must be at least 1 minute.`);
    if (!o.on_exhausted.length) problems.push(`${o.kind}: choose who hears about it when the attempts run out.`);
    const followUp = followUpIntentOf(o);
    const registered = broker.intents.find((i) => i.intent_id === followUp);
    if (!registered) problems.push(`${o.kind}: its follow-up intent ${followUp} is not in the catalog.`);
    else if (!registered.system) problems.push(`${o.kind}: its follow-up intent ${followUp} must be a system intent.`);
  }
  const types = new Set(broker.document_types.map((t) => t.type_id));
  broker.documents.forEach((r, i) => {
    if (!types.has(r.type_id)) problems.push(`Requirement ${i + 1}: unknown document type ${r.type_id}.`);
    if (r.opens_obligation && !kinds.includes(r.opens_obligation)) {
      problems.push(`Requirement ${i + 1}: it opens ${r.opens_obligation}, which is not an obligation type of this broker.`);
    }
    if (r.within_minutes !== null && r.within_minutes < 1) problems.push(`Requirement ${i + 1}: "within" must be at least 1 minute.`);
  });
  const limits = broker.limits;
  if (limits.max_steps < 0) problems.push("Max steps cannot be negative (0 is the single-plan mode).");
  if (limits.max_cost_usd <= 0) problems.push("The cost budget per run must be above $0.");
  if (limits.max_rejections < 0) problems.push("Max rejections cannot be negative.");
  return problems;
}

/** Which version the loads run, in words. */
export function rolloutSummary(configVersion: number, rollout: Rollout): string {
  const previous = rollout.previous_version;
  if (rollout.percent >= 100) return `Every load runs version ${configVersion}.`;
  if (previous === null) return `Every load runs version ${configVersion}: there is no previous version to split with.`;
  if (rollout.percent <= 0) return `Rolled back: every load runs version ${previous}.`;
  return `${rollout.percent}% of loads run version ${configVersion}, ${100 - rollout.percent}% stay on version ${previous}.`;
}
