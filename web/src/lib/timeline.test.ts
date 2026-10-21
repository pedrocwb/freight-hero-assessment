import { describe, expect, it } from "vitest";

import { event, outbox, run, snapshotA } from "./testFixtures";
import { buildTimeline, describeEvent, eventKind, groupActions, numberRuns, reviewPlan } from "./timeline";

const load = { contacts: [{ id: "drv_20931", role: "driver" as const, channels: ["sms" as const] }] };

describe("buildTimeline", () => {
  const timeline = buildTimeline(snapshotA);

  it("lists each inbound event in time order, without the agent's own actions", () => {
    expect(timeline.map((e) => e.event?.type)).toEqual(["risk_changed", "inbound_message", "timer_due"]);
  });

  it("follows each event with the run it caused, numbered by start", () => {
    expect(timeline.map((e) => e.runs.map((r) => `${r.number}:${r.run.run_id}`))).toEqual([["1:run_1"], ["2:run_2"], ["3:run_3"]]);
  });

  it("keeps an event that has no run yet, and a run whose event is not in the log", () => {
    const queued = event({ event_id: "m9", type: "inbound_message", occurred_at: "2026-10-13T07:40:00-07:00", channel: "sms" });
    const orphan = run({ run_id: "run_x", event_id: "gone", started_at: "2026-10-13T07:20:00-07:00" });
    const entries = buildTimeline({ events: [...snapshotA.events, queued], runs: [...snapshotA.runs, orphan] });
    expect(entries.map((e) => e.key)).toEqual(["e:risk:22776b45", "e:7a579362", "r:gone", "e:timer:timer_04352a60afe9", "e:m9"]);
    expect(entries[2]?.event).toBeNull();
    expect(entries[2]?.at).toBe("2026-10-13T07:20:00-07:00");
    expect(entries[4]?.runs).toEqual([]);
  });

  it("puts two runs of one event under it, in start order", () => {
    const again = run({ run_id: "run_1b", event_id: "risk:22776b45", started_at: "2026-10-13T07:02:00-07:00" });
    const entries = buildTimeline({ events: snapshotA.events, runs: [...snapshotA.runs, again] });
    expect(entries[0]?.runs.map((r) => r.run.run_id)).toEqual(["run_1", "run_1b"]);
    expect(entries[2]?.runs[0]?.number).toBe(4);
  });

  it("compares instants across offsets and keeps the log's order at the same instant", () => {
    const a = event({ event_id: "a", type: "inbound_message", occurred_at: "2026-10-13T15:00:00Z" });
    const b = event({ event_id: "b", type: "timer_due", occurred_at: "2026-10-13T07:59:00-07:00" });
    const c = event({ event_id: "c", type: "risk_changed", occurred_at: "2026-10-13T08:00:00-07:00" });
    expect(buildTimeline({ events: [a, b, c], runs: [] }).map((e) => e.key)).toEqual(["e:b", "e:a", "e:c"]);
  });
});

describe("numberRuns", () => {
  it("numbers runs from 1 by start time", () => {
    expect(numberRuns(snapshotA.runs).map((r) => r.run.run_id)).toEqual(["run_1", "run_2", "run_3"]);
  });
});

describe("describeEvent", () => {
  it("says who wrote, on which channel, with which files", () => {
    const message = snapshotA.events[2]!;
    expect(describeEvent(message, load)).toBe("Driver drv_20931 by SMS");
    const withFile = { ...message, attachments: [{ attachment_id: "a1", filename: "pod.pdf", content_type: "application/pdf", ref: "r" }] };
    expect(describeEvent(withFile, load)).toBe("Driver drv_20931 by SMS, with pod.pdf");
  });

  it("reads pings, timers and documents from their data", () => {
    expect(describeEvent(snapshotA.events[0]!, load)).toBe("Delay risk low to high");
    expect(describeEvent(snapshotA.events[3]!, load)).toBe("Follow-up timer due (ETA request)");
    const doc = event({ event_id: "doc:d1", type: "document_processed", occurred_at: "t", data: { type_id: "pod", status: "needs_review" } });
    expect(describeEvent(doc, load)).toBe("POD read by the pipeline (needs review)");
    expect(describeEvent(null, load)).toBe("An event missing from the log");
  });

  it("names the kind of each event", () => {
    expect(snapshotA.events.map(eventKind)).toEqual(["ping", "message", "message", "timer"]);
    expect(eventKind({ type: "document_processed" })).toBe("document");
  });
});

describe("groupActions", () => {
  it("groups one run's outbox by channel in a fixed order, oldest first", () => {
    const items = [
      outbox({ outbox_id: "o3", run_id: "r", kind: "hero_task", created_at: "2026-10-13T08:01:00-07:00" }),
      outbox({ outbox_id: "o2", run_id: "r", kind: "email", created_at: "2026-10-13T08:01:00-07:00" }),
      outbox({ outbox_id: "o4", run_id: "other", kind: "sms", created_at: "2026-10-13T08:01:00-07:00" }),
      outbox({ outbox_id: "o5", run_id: "r", kind: "tms_note", created_at: "2026-10-13T08:02:00-07:00" }),
      outbox({ outbox_id: "o1", run_id: "r", kind: "tms_note", created_at: "2026-10-13T08:00:00-07:00" }),
      outbox({ outbox_id: "o6", run_id: "r", kind: "stage_update", created_at: "2026-10-13T08:03:00-07:00" }),
    ];
    const groups = groupActions(items, "r");
    expect(groups.map((g) => [g.label, g.items.map((i) => i.outbox_id)])).toEqual([
      ["Email", ["o2"]],
      ["TMS note", ["o1", "o5"]],
      ["Hero task", ["o3"]],
      ["Other", ["o6"]],
    ]);
  });

  it("is empty for a run that sent nothing", () => {
    expect(groupActions(snapshotA.outbox, "run_none")).toEqual([]);
  });
});

describe("reviewPlan", () => {
  it("keeps the ledger changes and lists what the validator dropped", () => {
    const review = reviewPlan(snapshotA.runs[1]!);
    expect(review.ledger).toEqual([{ type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 }]);
    expect(review.dropped).toEqual([]);

    const refused = reviewPlan(snapshotA.runs[2]!);
    expect(refused.ledger).toEqual([]);
    expect(refused.dropped).toHaveLength(1);
    expect(refused.dropped[0]?.action).toMatchObject({ type: "send_sms", purpose: "eta_request" });
    expect(refused.dropped[0]?.violations[0]?.rule).toBe("one_eta_request_open");
  });

  it("keeps a violation that names no action, and a run without a plan", () => {
    const review = reviewPlan({ plan: null, violations: [{ rule: "budget", action_index: 4, message: "over", severity: "high" }] });
    expect(review.dropped).toEqual([{ action: null, violations: [{ rule: "budget", action_index: 4, message: "over", severity: "high" }] }]);
  });
});
