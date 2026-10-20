/** Helpers over the load metadata, mirroring `domain.load.Load` where the page needs it. */

import type { Contact, Load, Role, Stage, Stop } from "../api";

const PICKUP_STAGES: ReadonlySet<Stage> = new Set(["dispatched", "to_pickup", "at_pickup"]);

/** The stop the load is heading to or at; same rule as `Load.current_stop()`. */
export function currentStop(load: Load): Stop | undefined {
  if (load.current_stop_sequence !== null && load.current_stop_sequence !== undefined) {
    const found = load.stops.find((s) => s.sequence === load.current_stop_sequence);
    if (found) return found;
  }
  const wanted = PICKUP_STAGES.has(load.stage) ? "pickup" : "delivery";
  return load.stops.find((s) => s.type === wanted) ?? load.stops[load.stops.length - 1];
}

export function contactsWithRole(load: Load | undefined, role: Role): Contact[] {
  return load ? load.contacts.filter((c) => c.role === role) : [];
}

/** The contact who speaks for a role on a channel: the driver on SMS, the broker on Slack. */
export function contactFor(
  load: Load | undefined,
  role: Role,
  channel: Contact["channels"][number],
): Contact | undefined {
  const withRole = contactsWithRole(load, role);
  return withRole.find((c) => c.channels.includes(channel)) ?? withRole[0];
}

export const ROLE_LABEL: Record<string, string> = {
  driver: "Driver",
  dispatcher: "Dispatcher",
  broker: "Broker",
  agent: "Agent",
};

export function roleLabel(role: string | null | undefined): string {
  if (!role) return "Unknown sender";
  return ROLE_LABEL[role] ?? role.charAt(0).toUpperCase() + role.slice(1);
}

export const STAGE_LABEL: Record<Stage, string> = {
  dispatched: "Dispatched",
  to_pickup: "To pickup",
  at_pickup: "At pickup",
  to_delivery: "To delivery",
  at_delivery: "At delivery",
  delivered: "Delivered",
};

export function stageLabel(stage: string): string {
  return STAGE_LABEL[stage as Stage] ?? stage;
}

const ACRONYMS: Record<string, string> = {
  eta: "ETA",
  sop: "SOP",
  tms: "TMS",
  po: "PO",
  sms: "SMS",
  llm: "LLM",
  pod: "POD",
  bol: "BOL",
  mc: "MC",
};

/** "delay_risk_high" reads as "Delay risk high"; "eta_request" as "ETA request". */
export function humanize(id: string): string {
  const words = id.replace(/_/g, " ").trim().split(/\s+/).map((w) => ACRONYMS[w.toLowerCase()] ?? w);
  const text = words.join(" ").replace(/\bfollow up\b/g, "follow-up");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const CHANNEL_LABEL: Record<string, string> = { sms: "SMS", email: "email", slack: "Slack", system: "system", hero: "Hero" };

export function channelLabel(channel: string): string {
  return CHANNEL_LABEL[channel] ?? channel;
}
