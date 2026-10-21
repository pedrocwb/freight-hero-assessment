import { describe, expect, it } from "vitest";

import type { RecordedStep } from "../api";
import { draftSteps, stepBody, suggestId, suggestTags, toDraft } from "./scenarioDraft";
import { outbox, run } from "./testFixtures";

const TZ = "America/Los_Angeles";
const LOAD = { contacts: [{ id: "drv_20931", role: "driver" as const, channels: ["sms" as const] }] };

function recorded(over: Partial<RecordedStep> & Pick<RecordedStep, "index" | "at" | "kind">): RecordedStep {
  return {
    message: null,
    ping: null,
    latency_seconds: 60,
    run_ids: [],
    run_statuses: [],
    outbox_ids: [],
    llm_calls: 0,
    obligations: {},
    timers: { pending: 0, due_at: null },
    ...over,
  };
}

const RECORDING: RecordedStep[] = [
  recorded({
    index: 0,
    at: "2026-10-13T07:00:00-07:00",
    kind: "ping",
    ping: { delay_risk: "high", lat: 0, lon: 0 },
    run_ids: ["r1"],
    run_statuses: ["acted"],
    outbox_ids: ["o1"],
    llm_calls: 1,
    obligations: { eta_request: { status: "open", attempts: 1, max_attempts: 2 } },
    timers: { pending: 1, due_at: "07:31" },
  }),
  recorded({
    index: 1,
    at: "2026-10-13T07:05:00-07:00",
    kind: "message",
    message: { channel: "sms", sender: "drv_20931", body: "ok", attachments: [] },
    run_ids: ["r2"],
    run_statuses: ["noop"],
    llm_calls: 1,
    obligations: { eta_request: { status: "open", attempts: 1, max_attempts: 2 } },
    timers: { pending: 1, due_at: "07:31" },
  }),
];
const OUTBOX = [outbox({ outbox_id: "o1", run_id: "r1", kind: "sms", created_at: "2026-10-13T07:01:00-07:00", payload: { contact_id: "drv_20931", purpose: "eta_request" } })];

describe("draftSteps", () => {
  const steps = draftSteps(RECORDING, OUTBOX, LOAD, TZ);

  it("drafts each recorded step with its time, a note and the expectations of what happened", () => {
    expect(steps.map((s) => [s.at, s.kind, s.note])).toEqual([
      ["07:00", "ping", "Location ping, high delay risk"],
      ["07:05", "message", "Driver by SMS: “ok”"],
    ]);
    expect(steps[0]!.expectations.map((e) => e.label)).toEqual([
      "1 SMS to the driver",
      "ETA request open, 1 attempt",
      "1 timer pending, next at 07:31",
      "The agent acted",
      "1 model call",
    ]);
  });

  it("drafts a do-nothing step: nothing sent, the run did nothing", () => {
    expect(stepBody(steps[1]!).expect).toEqual({
      no_actions: true,
      obligations: { eta_request: { status: "open", attempts: 1 } },
      timers: { pending: 1, due_at: "07:31" },
      run_status: ["noop"],
      llm_calls: 1,
    });
  });

  it("leaves out an unchecked expectation and adds the chosen forbid lines", () => {
    const step = { ...steps[0]!, forbid: ["email" as const, "eta_request" as const] };
    step.expectations = step.expectations.map((e) => (e.field === "llm_calls" ? { ...e, on: false } : e));
    expect(stepBody(step)).toEqual({
      expect: {
        actions: [{ kind: "sms", to: "drv_20931", count: 1 }],
        obligations: { eta_request: { status: "open", attempts: 1 } },
        timers: { pending: 1, due_at: "07:31" },
        run_status: ["acted"],
      },
      forbid: [{ kind: "email" }, { kind: "sms", purpose: "eta_request" }],
    });
  });

  it("does not hold the model calls when another load ran in the same step", () => {
    const [step] = draftSteps([{ ...RECORDING[0]!, llm_calls: null }], OUTBOX, LOAD, TZ);
    expect(step!.expectations.some((e) => e.field === "llm_calls")).toBe(false);
  });
});

describe("the draft's body", () => {
  it("tags the SOP sections the runs used, and suggests an id from the load and the time", () => {
    const runs = [run({ run_id: "r1", event_id: "e", started_at: "x", sop_section_ids: ["delay_risk_high", "communication_rules"] }), run({ run_id: "other", event_id: "e", started_at: "x", sop_section_ids: ["pod"] })];
    expect(suggestTags(runs, RECORDING)).toEqual(["regression", "sop:delay_risk_high", "sop:communication_rules"]);
    expect(suggestId("481207", "2026-10-13T07:31:00-07:00", TZ)).toBe("recorded_481207_0731");
  });

  it("is the shape POST /api/scenarios takes", () => {
    const steps = draftSteps(RECORDING, OUTBOX, LOAD, TZ);
    const draft = toDraft({ loadId: "481207", id: " my_case ", title: "My case", suite: "", tags: "regression, sop:x", description: "", steps });
    expect({ ...draft, steps: draft.steps.map((s) => [s.index, s.note]) }).toEqual({
      load_id: "481207",
      id: "my_case",
      title: "My case",
      suite: "core",
      tags: ["regression", "sop:x"],
      description: "",
      steps: [
        [0, "Location ping, high delay risk"],
        [1, "Driver by SMS: “ok”"],
      ],
    });
  });
});
