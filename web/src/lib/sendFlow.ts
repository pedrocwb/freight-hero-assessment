/**
 * Send and wait: the composer's state while an input travels. The person sends; the chat
 * shows their message at once with "the agent is thinking"; the API lets the latency pass
 * and runs the queue; the answer is the runs that input caused. Then the chat says what
 * came of it: a reply, a quiet action, nothing, or no run at all.
 */

import type { OutboxItem, RunRecord } from "../api";

export type TurnKind = "message" | "file" | "ping" | "timer";

/** What the person sent, shown in the chat before the API answers. */
export interface PendingMessage {
  from: string;
  channel: string;
  body: string;
  attachments: string[];
}

export type Outcome = "replied" | "acted" | "nothing" | "no_run" | "failed";

export type FlowState =
  | { phase: "idle" }
  | { phase: "waiting"; turn: TurnKind; label: string; pending: PendingMessage | null }
  | { phase: "done"; turn: TurnKind; outcome: Outcome; runIds: string[]; summary: string }
  | { phase: "failed"; turn: TurnKind; error: string };

export type FlowAction =
  | { type: "start"; turn: TurnKind; label: string; pending?: PendingMessage | null }
  | { type: "finish"; runs: readonly RunRecord[]; outbox: readonly Pick<OutboxItem, "run_id" | "kind">[]; duplicate?: boolean }
  | { type: "fail"; error: string }
  | { type: "dismiss" };

export const IDLE: FlowState = { phase: "idle" };

const MESSAGE_KINDS: ReadonlySet<string> = new Set(["sms", "email", "broker_slack"]);

/** What a turn's runs came to, from the outbox items they made. */
export function outcomeOf(runs: readonly RunRecord[], outbox: readonly Pick<OutboxItem, "run_id" | "kind">[]): Outcome {
  if (!runs.length) return "no_run";
  const ids = new Set(runs.map((r) => r.run_id));
  const made = outbox.filter((o) => ids.has(o.run_id));
  if (made.some((o) => MESSAGE_KINDS.has(o.kind))) return "replied";
  if (runs.every((r) => r.status === "failed")) return "failed";
  if (made.length || runs.some((r) => r.status === "acted" || r.status === "escalated")) return "acted";
  return "nothing";
}

function summary(turn: TurnKind, outcome: Outcome, duplicate: boolean): string {
  if (duplicate) return "Already received: nothing ran again.";
  switch (outcome) {
    case "replied":
      return "The agent answered.";
    case "acted":
      return "The agent acted without writing to anyone.";
    case "nothing":
      return "The agent decided to do nothing.";
    case "failed":
      return "The run failed.";
    case "no_run":
      if (turn === "ping") return "The risk band did not change, so the agent was not woken.";
      if (turn === "timer") return "No timer was due: nothing ran.";
      return "Nothing ran.";
  }
}

export function flowReducer(state: FlowState, action: FlowAction): FlowState {
  switch (action.type) {
    case "start":
      if (state.phase === "waiting") return state; // one input at a time
      return { phase: "waiting", turn: action.turn, label: action.label, pending: action.pending ?? null };
    case "finish": {
      if (state.phase !== "waiting") return state;
      const outcome = outcomeOf(action.runs, action.outbox);
      return {
        phase: "done",
        turn: state.turn,
        outcome,
        runIds: action.runs.map((r) => r.run_id),
        summary: summary(state.turn, outcome, Boolean(action.duplicate)),
      };
    }
    case "fail":
      return state.phase === "waiting" ? { phase: "failed", turn: state.turn, error: action.error } : state;
    case "dismiss":
      return state.phase === "waiting" ? state : IDLE;
  }
}

export function isWaiting(state: FlowState): state is Extract<FlowState, { phase: "waiting" }> {
  return state.phase === "waiting";
}
