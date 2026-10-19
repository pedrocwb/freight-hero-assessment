/**
 * Conversation threads: the server's merged list plus what the person just sent.
 *
 * A message written in a composer shows at once as "sending". It stays until the server's
 * list holds the same text from the same contact, then the server's copy replaces it, so a
 * refresh never shows the message twice and a failed send never silently disappears.
 */

import type { ConversationItem, OutboxItem } from "../api";
import { formatDay, localDayKey } from "./time";

export interface PendingMessage {
  localId: string;
  at: string;
  who: string;
  role: string;
  text: string;
  /** sending: request in flight; sent: the API accepted it, the thread has not shown it yet. */
  state: "sending" | "sent" | "failed";
  error?: string;
}

export interface ThreadEntry {
  key: string;
  at: string;
  direction: "in" | "out";
  who: string;
  role: string;
  text: string;
  subject: string | null;
  status: string | null;
  pending: PendingMessage["state"] | null;
  error: string | null;
}

function time(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

const normalize = (text: string) => text.trim().replace(/\s+/g, " ");

/** True when the server already holds the message the person sent. */
export function isConfirmed(pending: PendingMessage, server: readonly ConversationItem[]): boolean {
  return server.some(
    (item) =>
      item.direction === "in" &&
      item.who === pending.who &&
      normalize(item.text) === normalize(pending.text),
  );
}

/** Pending messages the server has not confirmed yet. */
export function unconfirmed(
  pending: readonly PendingMessage[],
  server: readonly ConversationItem[],
): PendingMessage[] {
  return pending.filter((p) => !isConfirmed(p, server));
}

/**
 * One time-ordered thread. Ties keep the server's order, and a pending message sorts after
 * server items of the same instant, which is where it lands once confirmed.
 */
export function mergeConversation(
  server: readonly ConversationItem[],
  pending: readonly PendingMessage[] = [],
): ThreadEntry[] {
  const entries: (ThreadEntry & { order: number })[] = server.map((item, index) => ({
    key: `s:${index}:${item.at}:${item.who}`,
    at: item.at,
    direction: item.direction,
    who: item.who,
    role: item.role,
    text: item.text,
    subject: item.subject ?? null,
    status: item.status ?? null,
    pending: null,
    error: null,
    order: index,
  }));
  unconfirmed(pending, server).forEach((p, index) => {
    entries.push({
      key: `p:${p.localId}`,
      at: p.at,
      direction: "in",
      who: p.who,
      role: p.role,
      text: p.text,
      subject: null,
      status: null,
      pending: p.state,
      error: p.error ?? null,
      order: server.length + index,
    });
  });
  entries.sort((a, b) => time(a.at) - time(b.at) || a.order - b.order);
  return entries.map(({ order: _order, ...entry }) => entry);
}

export interface DayGroup<T> {
  key: string;
  label: string;
  items: T[];
}

/** Consecutive entries grouped by their local day, for the day separators of a thread. */
export function groupByDay<T extends { at: string }>(items: readonly T[], timeZone: string): DayGroup<T>[] {
  const groups: DayGroup<T>[] = [];
  for (const item of items) {
    const key = localDayKey(item.at, timeZone);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, label: formatDay(item.at, timeZone), items: [item] });
  }
  return groups;
}

/**
 * The recipients of an email the agent sent. The conversation endpoint does not carry
 * them, so they are read from the outbox item with the same body.
 */
export function emailRecipients(entry: Pick<ThreadEntry, "text" | "direction">, outbox: readonly OutboxItem[]): string[] {
  if (entry.direction !== "out") return [];
  const match = outbox.find(
    (item) =>
      item.kind === "email" &&
      typeof item.payload.body === "string" &&
      normalize(item.payload.body) === normalize(entry.text),
  );
  const to = match?.payload.to_contact_ids;
  return Array.isArray(to) ? to.filter((id): id is string => typeof id === "string") : [];
}

/** Initials for a sender avatar: "AG" for the agent, "BR" for a broker contact. */
export function initials(who: string, role: string): string {
  if (who === "agent") return "AG";
  const source = role || who;
  return source.slice(0, 2).toUpperCase();
}
