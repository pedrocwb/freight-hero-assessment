import { describe, expect, it } from "vitest";

import { buildConversation, describeEvent, type ChatMarker, type ChatMessage } from "./conversation";
import { event, outbox, run, snapshotA } from "./testFixtures";

const LOAD = {
  contacts: [
    { id: "drv_20931", role: "driver" as const, channels: ["sms" as const] },
    { id: "dsp_4410", role: "dispatcher" as const, channels: ["email" as const] },
  ],
};

describe("buildConversation", () => {
  const chat = buildConversation(snapshotA, LOAD);

  it("puts inbound messages, the agent's messages and system events in time order", () => {
    expect(chat.map((c) => [c.kind, c.at.slice(11, 16), c.kind === "message" ? c.side : (c as ChatMarker).tone])).toEqual([
      ["marker", "07:00", "alert"],
      ["message", "07:01", "out"],
      ["message", "07:12", "in"],
      ["message", "07:13", "out"],
      ["marker", "07:31", "system"],
      ["message", "07:31", "out"],
    ]);
  });

  it("keeps the log's order at one instant: the ETA request before the message logged after it", () => {
    const t0 = "2026-10-13T06:55:00-07:00";
    const t1 = "2026-10-13T06:56:00-07:00";
    const items = buildConversation(
      {
        events: [
          event({ event_id: "p", type: "risk_changed", occurred_at: t0, data: { previous: "low", current: "high" } }),
          event({ event_id: "a", type: "agent_action", occurred_at: t1, direction: "outbound", data: { outbox_id: "o1" } }),
          event({ event_id: "m", type: "inbound_message", occurred_at: t1, channel: "sms", sender_contact_id: "drv_20931", body: "PO?" }),
        ],
        runs: [run({ run_id: "r1", event_id: "p", started_at: t1 })],
        outbox: [outbox({ outbox_id: "o1", run_id: "r1", kind: "sms", created_at: t1, payload: { contact_id: "drv_20931", text: "ETA?" } })],
      },
      LOAD,
    );
    expect(items.map((i) => i.key)).toEqual(["e:p", "o:o1", "e:m"]);
  });

  it("names the sender, the recipient, the channel and why the agent wrote", () => {
    const [, ask, question] = chat as ChatMessage[];
    expect([ask!.from, ask!.to, ask!.channel, ask!.purpose, ask!.runId]).toEqual(["Agent", "the driver", "SMS", "ETA request", "run_1"]);
    expect([question!.from, question!.channel, question!.body, question!.runId]).toEqual(["Driver", "SMS", "What's the PO number?", "run_2"]);
  });

  it("leaves out the agent's own actions in the event log: the outbox has them", () => {
    expect(chat.filter((c) => c.key.startsWith("e:act:"))).toEqual([]);
  });

  it("shows a run that sent nothing as a marker that says it did nothing, with why", () => {
    const quiet = buildConversation(
      {
        events: [event({ event_id: "m1", type: "inbound_message", occurred_at: "2026-10-13T07:05:00-07:00", channel: "sms", sender_contact_id: "drv_20931", body: "ok" })],
        runs: [run({ run_id: "r1", event_id: "m1", started_at: "2026-10-13T07:06:00-07:00", status: "noop", gate: { intent: "acknowledgement", act: false, reason: "Just an ok." } })],
        outbox: [],
      },
      LOAD,
    );
    expect(quiet.at(-1)).toMatchObject({ kind: "marker", tone: "nothing", text: "The agent did nothing", detail: "Just an ok.", runId: "r1" });
  });

  it("turns a Hero task, a TMS note and a stage change into quiet markers, an email into a message", () => {
    const at = "2026-10-13T08:01:00-07:00";
    const items = buildConversation(
      {
        events: [],
        runs: [run({ run_id: "r4", event_id: "t2", started_at: at })],
        outbox: [
          outbox({ outbox_id: "o1", run_id: "r4", kind: "email", created_at: at, payload: { to_contact_ids: ["dsp_4410"], subject: "ETA needed", body: "Please send it." } }),
          outbox({ outbox_id: "o2", run_id: "r4", kind: "hero_task", created_at: at, payload: { title: "Call the driver", urgent: true } }),
          outbox({ outbox_id: "o3", run_id: "r4", kind: "tms_note", created_at: at, payload: { text: "Escalated." } }),
          outbox({ outbox_id: "o4", run_id: "r4", kind: "stage_update", created_at: at, payload: { stage: "at_pickup" } }),
        ],
      },
      LOAD,
    );
    expect(items.map((i) => (i.kind === "message" ? `${i.channel} to ${i.to}: ${i.subject}` : i.text))).toEqual([
      "Email to the dispatcher: ETA needed",
      "Urgent Hero task: Call the driver",
      "TMS note",
      "Stage → At pickup",
    ]);
  });
});

describe("describeEvent", () => {
  it("says what woke the agent in plain words", () => {
    const [ping, , question, timer] = snapshotA.events;
    expect(describeEvent(ping!, LOAD)).toBe("Delay risk went from low to high");
    expect(describeEvent(question!, LOAD)).toBe("Driver wrote by SMS: “What's the PO number?”");
    expect(describeEvent(timer!, LOAD)).toBe("The follow-up timer for the ETA request fired");
  });
});
