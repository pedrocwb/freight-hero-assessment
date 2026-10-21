import { describe, expect, it } from "vitest";

import { describeAction, explainRun, layerOf, leftOpen, plainIds } from "./why";
import { run, snapshotA } from "./testFixtures";

const LOAD = { contacts: [{ id: "drv_20931", role: "driver" as const, channels: ["sms" as const] }] };
const SECTIONS = new Map([
  ["driver_questions", { title: "Driver questions", sources: ["standard/driver_questions.md", "clients/northline/driver_questions.md"] }],
  ["communication_rules", { title: "Communication rules", sources: ["standard/communication.md"] }],
]);
const AFTER = {
  obligations: { eta_request: { status: "open" as const, attempts: 1, max_attempts: 2 } },
  timers: { pending: 1, due_at: "07:31" },
};

describe("explainRun", () => {
  const question = snapshotA.events[2]!;
  const answered = run({
    run_id: "run_2",
    event_id: question.event_id,
    started_at: "2026-10-13T07:13:00-07:00",
    intent: "question",
    gate: { intent: "question", act: true, reason: "The driver asks for the PO." },
    gate_scores: { model: "jev", intents: { question: 0.97 }, act: 0.91, threshold: 0.5 },
    sop_section_ids: ["driver_questions", "communication_rules"],
    trajectory: [
      { tool: "search_sop", args: { query: "PO" }, result: "2 hits", accepted: null, cost_usd: 0 },
      { tool: "send_sms", args: { contact_id: "drv_99999", text: "x", purpose: "reply" }, result: "rejected by recipient_is_contact: not a contact of load 481207", accepted: false, cost_usd: 0 },
      { tool: "send_sms", args: { contact_id: "drv_20931", text: "The PO number is 55821.", purpose: "reply" }, result: "accepted as action #1", accepted: true, cost_usd: 0 },
    ],
    plan: { rationale: "Answered the PO from the load's instructions.", actions: [] },
  });
  const why = explainRun(answered, { event: question, load: LOAD, sections: SECTIONS, step: AFTER });

  it("says what the agent understood: the event, the intent and the gate's call with its probabilities", () => {
    expect(why.understood).toEqual({
      event: "Driver wrote by SMS: “What's the PO number?”",
      intent: "question",
      gate: "Needs the agent",
      actProbability: 0.91,
      intentProbability: 0.97,
    });
  });

  it("gives a chat model gate's reason in words", () => {
    const chat = explainRun({ ...answered, gate_scores: null }, { event: question, load: LOAD, sections: SECTIONS, step: AFTER });
    expect([chat.understood.gate, chat.understood.actProbability]).toEqual(["Needs the agent: the driver asks for the PO.", null]);
  });

  it("lists the SOP sections it followed with their layer", () => {
    expect(why.followed).toEqual([
      { id: "driver_questions", title: "Driver questions", layer: "standard, extended by the client" },
      { id: "communication_rules", title: "Communication rules", layer: "standard" },
    ]);
  });

  it("lists each action in order, a refused one with the rule's reason, and no read tool", () => {
    expect(why.did).toEqual([
      { text: "Text drv_99999: “x”", ok: false, reason: "Recipient is contact: not a contact of load 481207" },
      { text: "Text the driver: “The PO number is 55821.”", ok: true, reason: null },
    ]);
  });

  it("says what it left open and quotes its own answer", () => {
    expect(why.leftOpen).toEqual(["ETA request open, 1 of 2 attempts", "Next follow-up at 07:31"]);
    expect(why.rationale).toBe("Answered the PO from the load's instructions.");
    expect(why.headline).toBe("The agent acted");
  });

  it("falls back to the plan and its violations for a run without a trajectory", () => {
    const older = explainRun(snapshotA.runs[2]!, { event: question, load: LOAD, sections: new Map(), step: null });
    expect(older.did.map((d) => [d.ok, d.reason])).toEqual([
      [true, null],
      [false, "One ETA request open: an ETA request is already open"],
    ]);
    expect(older.leftOpen).toBeNull();
    expect(older.followed).toEqual([]);
  });

  it("explains a run that did nothing with the gate's reason", () => {
    const quiet = explainRun(
      run({ run_id: "r", event_id: "e", started_at: "2026-10-13T07:06:00-07:00", status: "noop", gate: { intent: "acknowledgement", act: false, reason: "Just an ok." } }),
      { event: null, load: LOAD, sections: new Map(), step: { obligations: {}, timers: { pending: 0, due_at: null } } },
    );
    expect([quiet.headline, quiet.understood.gate, quiet.did, quiet.leftOpen]).toEqual([
      "The agent did nothing",
      "Needs no action: just an ok.",
      [],
      ["Nothing open"],
    ]);
  });
});

describe("plain lines", () => {
  it("describes the ledger actions", () => {
    expect(describeAction("schedule_timer", { kind: "eta_request", due_in_minutes: 30 }, LOAD)).toBe("Follow up on the ETA request in 30 min");
    expect(describeAction("close_obligation", { kind: "eta_request", status: "exhausted" }, LOAD)).toBe("Close the ETA request as exhausted");
    expect(describeAction("create_hero_task", { title: "Call the driver", urgent: true }, LOAD)).toBe("Create an urgent Hero task: “Call the driver”");
  });

  it("says when the requests ran out", () => {
    expect(leftOpen({ obligations: { eta_request: { status: "exhausted", attempts: 2, max_attempts: 2 } }, timers: { pending: 0, due_at: null } })).toEqual(["Nothing open"]);
  });

  it("reads ids inside a sentence as words", () => {
    expect(plainIds("eta_request follow-up timer is due")).toBe("ETA request follow-up timer is due");
    expect(plainIds("The driver asks for the PO.")).toBe("The driver asks for the PO.");
  });

  it("names the SOP layer of a section", () => {
    expect(layerOf(["clients/keystone/reefer.md"])).toBe("client");
    expect(layerOf(["standard/types/reefer.md"])).toBe("load type");
  });
});
