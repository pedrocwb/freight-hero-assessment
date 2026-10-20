/**
 * Refresh topics. Panels subscribe to the topics their data depends on; stream events,
 * the polling fallback and the page's own writes publish them. Bursts (an advance of 30
 * minutes fires several runs) are coalesced so each panel refetches once.
 */

export type Topic =
  | "state" // sim clock, queue, load summaries
  | "snapshot" // the selected load: ledger, timers, events, outbox, runs
  | "conversation"
  | "tms"
  | "tasks";

export const ALL_TOPICS: readonly Topic[] = [
  "state",
  "snapshot",
  "conversation",
  "tms",
  "tasks",
];

type Listener = () => void;

const listeners = new Map<Topic, Set<Listener>>();
const queued = new Set<Topic>();
let flushTimer: ReturnType<typeof setTimeout> | null = null;

const COALESCE_MS = 120;

function flush(): void {
  flushTimer = null;
  const called = new Set<Listener>();
  for (const topic of queued) {
    for (const listener of listeners.get(topic) ?? []) {
      if (called.has(listener)) continue;
      called.add(listener);
      listener();
    }
  }
  queued.clear();
}

export function invalidate(...topics: Topic[]): void {
  for (const topic of topics) queued.add(topic);
  if (flushTimer === null) flushTimer = setTimeout(flush, COALESCE_MS);
}

export function subscribe(topics: readonly Topic[], listener: Listener): () => void {
  for (const topic of topics) {
    let set = listeners.get(topic);
    if (!set) listeners.set(topic, (set = new Set()));
    set.add(listener);
  }
  return () => {
    for (const topic of topics) listeners.get(topic)?.delete(listener);
  };
}
