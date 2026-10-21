import { describe, expect, it } from "vitest";

import { flowReducer, IDLE, outcomeOf, type FlowState } from "./sendFlow";
import { run } from "./testFixtures";

const AT = "2026-10-13T07:13:00-07:00";
const pending = { from: "Driver", channel: "SMS", body: "What's the PO number?", attachments: [] };

function waiting(turn: "message" | "ping" | "timer" = "message"): FlowState {
  return flowReducer(IDLE, { type: "start", turn, label: "Sending", pending: turn === "message" ? pending : null });
}

describe("flowReducer", () => {
  it("shows the message at once while it waits, and one input at a time", () => {
    const state = waiting();
    expect(state).toEqual({ phase: "waiting", turn: "message", label: "Sending", pending });
    expect(flowReducer(state, { type: "start", turn: "ping", label: "Again" })).toBe(state);
    expect(flowReducer(state, { type: "dismiss" })).toBe(state);
  });

  it("ends with the agent's answer and the runs to explain", () => {
    const done = flowReducer(waiting(), {
      type: "finish",
      runs: [run({ run_id: "r2", event_id: "m", started_at: AT })],
      outbox: [{ run_id: "r2", kind: "sms" }],
    });
    expect(done).toEqual({ phase: "done", turn: "message", outcome: "replied", runIds: ["r2"], summary: "The agent answered." });
    expect(flowReducer(done, { type: "dismiss" })).toEqual(IDLE);
  });

  it("says when the agent decided to do nothing, and when a ping woke nothing", () => {
    const nothing = flowReducer(waiting(), { type: "finish", runs: [run({ run_id: "r", event_id: "m", started_at: AT, status: "noop" })], outbox: [] });
    const quiet = flowReducer(waiting("ping"), { type: "finish", runs: [], outbox: [] });
    expect(nothing).toMatchObject({ outcome: "nothing", summary: "The agent decided to do nothing." });
    expect(quiet).toMatchObject({ outcome: "no_run", summary: "The risk band did not change, so the agent was not woken." });
  });

  it("says a duplicate ran nothing again, and keeps a failure's message", () => {
    expect(flowReducer(waiting(), { type: "finish", runs: [], outbox: [], duplicate: true })).toMatchObject({ summary: "Already received: nothing ran again." });
    expect(flowReducer(waiting("timer"), { type: "fail", error: "API down" })).toEqual({ phase: "failed", turn: "timer", error: "API down" });
    expect(flowReducer(IDLE, { type: "fail", error: "late" })).toBe(IDLE);
  });
});

describe("outcomeOf", () => {
  it("tells a reply from a quiet action and from a failure", () => {
    const acted = run({ run_id: "r", event_id: "e", started_at: AT });
    expect(outcomeOf([acted], [{ run_id: "r", kind: "hero_task" }])).toBe("acted");
    expect(outcomeOf([acted], [])).toBe("acted");
    expect(outcomeOf([run({ run_id: "f", event_id: "e", started_at: AT, status: "failed" })], [])).toBe("failed");
    expect(outcomeOf([acted], [{ run_id: "other", kind: "sms" }])).toBe("acted");
  });
});
