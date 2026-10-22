import { describe, expect, it } from "vitest";

import { conversation, progress, remaining, type DurableSnapshot } from "./durable";
import { event, outbox, run } from "./testFixtures";

function completed(): Pick<DurableSnapshot, "outbox" | "obligations" | "timers" | "runs"> {
  const at = "2026-10-21T12:00:00Z";
  return {
    outbox: Array.from({ length: 11 }, (_, i) => ({
      ...outbox({ outbox_id: String(i), run_id: String(i), kind: i === 10 ? "hero_task" : "sms", created_at: at, payload: { purpose: "eta_request" } }),
      accepted_at: at,
    })),
    obligations: [{ obligation_id: "eta", load_id: "aurora-test", kind: "eta_request", status: "exhausted", attempts: 10, opened_at: at, last_action_at: at, deferred_until: null, deferrals_used: 0, active_timer_id: null, sop_version: null }],
    timers: [], runs: [],
  };
}

describe("durable operation", () => {
  it("uses the SOP pinned to the load for its progress, including custom attempt limits", () => {
    const state = completed();
    state.outbox = [state.outbox[0]!, state.outbox[1]!, state.outbox[10]!];
    const load = { sop_snapshot: { version: "v2", policy: { max_attempts: 2, interval_minutes: 3, body: "custom" } } } as DurableSnapshot["load"];
    expect(progress({ ...state, load })).toMatchObject({ complete: true, maxAttempts: 2, intervalMinutes: 3 });
    expect(progress(state).complete).toBe(false);
    state.outbox[1]!.status = "cancelled";
    expect(progress({ ...state, load }).sms).toHaveLength(1);
    expect(progress({ ...state, load }).complete).toBe(false);
  });
  it("interleaves a driver's message with confirmed outgoing SMS", () => {
    const rows = conversation({
      outbox: [{ ...outbox({ outbox_id: "sms-1", run_id: "run-1", kind: "sms", created_at: "2026-10-21T12:00:00Z" }), accepted_at: "2026-10-21T12:00:03Z" }],
      events: [
        event({ event_id: "driver-1", type: "inbound_message", occurred_at: "2026-10-21T12:00:05Z", sender_contact_id: "drv_aurora", body: "Chego em 20 minutos." }),
        event({ event_id: "risk", type: "risk_changed", occurred_at: "2026-10-21T12:00:00Z" }),
      ],
    });
    expect(rows.map((r) => r.kind)).toEqual(["outbound", "driver"]);
    expect(rows.map((r) => r.key)).toEqual(["sms-1", "driver-1"]);
  });
  it("requires ten confirmed SMS and a confirmed hero task to finish", () => {
    const state = completed();
    expect(progress(state).complete).toBe(true);
    state.outbox[10]!.status = "pending";
    expect(progress(state).complete).toBe(false);
    state.outbox[10]!.status = "sent";
    state.outbox[9]!.status = "pending";
    expect(progress(state).sms).toHaveLength(9);
    expect(progress(state).complete).toBe(false);
  });

  it("does not mistake a model-failure Hero for a successful SOP escalation", () => {
    const state = completed();
    state.runs = [run({ run_id: "error", event_id: "event", started_at: "2026-10-21T12:00:00Z", error: "llm_error: unavailable", status: "escalated" })];
    expect(progress(state).failed).toBeTruthy();
    expect(progress(state).complete).toBe(false);
  });

  it("counts only ETA requests, and rejects duplicate Hero escalation as completion", () => {
    const state = completed();
    state.outbox[9]!.payload.purpose = "reply";
    expect(progress(state).sms).toHaveLength(9);
    expect(progress(state).complete).toBe(false);
    state.outbox[9]!.payload.purpose = "eta_request";
    state.outbox.push({ ...state.outbox[10]!, outbox_id: "another-hero" });
    expect(progress(state).complete).toBe(false);
  });

  it("cannot complete while a follow-up is still pending", () => {
    const state = completed();
    state.timers = [{ timer_id: "pending", load_id: "aurora-test", obligation_id: "eta", due_at: "2026-10-21T12:01:00Z", created_at: "2026-10-21T12:00:00Z", reason: "follow up", status: "pending" }];
    expect(progress(state).complete).toBe(false);
  });

  it("counts down absolute deadlines and shows zero while an overdue event processes", () => {
    expect(remaining("2026-10-21T12:01:00Z", Date.parse("2026-10-21T12:00:30Z"))).toBe("0:30");
    expect(remaining("2026-10-21T12:01:00Z", Date.parse("2026-10-21T12:01:01Z"))).toBe("0:00");
  });
});
