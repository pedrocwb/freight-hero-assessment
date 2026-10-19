import { describe, expect, it } from "vitest";

import type { ConversationItem, OutboxItem } from "../api";
import { emailRecipients, groupByDay, mergeConversation, type PendingMessage } from "./conversation";

const LA = "America/Los_Angeles";

const item = (at: string, direction: "in" | "out", who: string, text: string): ConversationItem => ({
  at,
  direction,
  channel: "sms",
  who,
  role: who === "agent" ? "agent" : "driver",
  text,
});

const pending = (localId: string, at: string, text: string, state: PendingMessage["state"] = "sending"): PendingMessage => ({
  localId,
  at,
  who: "drv_20931",
  role: "driver",
  text,
  state,
});

describe("mergeConversation", () => {
  const server = [
    item("2026-10-13T07:13:00-07:00", "out", "agent", "The PO is 55821."),
    item("2026-10-13T07:01:00-07:00", "out", "agent", "What time will you arrive?"),
    item("2026-10-13T07:12:00-07:00", "in", "drv_20931", "What's the PO number?"),
  ];

  it("orders the thread by time", () => {
    const thread = mergeConversation(server);
    expect(thread.map((e) => e.at.slice(11, 16))).toEqual(["07:01", "07:12", "07:13"]);
    expect(thread.every((e) => e.pending === null)).toBe(true);
  });

  it("compares instants, not strings, across offsets", () => {
    const thread = mergeConversation([
      item("2026-10-13T15:00:00Z", "in", "drv_20931", "late"), // 08:00 PDT
      item("2026-10-13T07:30:00-07:00", "out", "agent", "early"),
    ]);
    expect(thread.map((e) => e.text)).toEqual(["early", "late"]);
  });

  it("shows a message being sent until the server holds it", () => {
    const sent = pending("p1", "2026-10-13T07:20:00-07:00", "ETA 9:40");
    const before = mergeConversation(server, [sent]);
    expect(before).toHaveLength(4);
    expect(before[3]).toMatchObject({ text: "ETA 9:40", pending: "sending", direction: "in" });

    const confirmed = [...server, item("2026-10-13T07:20:00-07:00", "in", "drv_20931", "ETA  9:40 ")];
    const after = mergeConversation(confirmed, [sent]);
    expect(after).toHaveLength(4);
    expect(after.filter((e) => e.text.includes("9:40")).map((e) => e.pending)).toEqual([null]);
  });

  it("keeps a failed send with its error", () => {
    const failed = { ...pending("p2", "2026-10-13T07:21:00-07:00", "hello", "failed"), error: "boom" };
    const thread = mergeConversation(server, [failed]);
    expect(thread.at(-1)).toMatchObject({ pending: "failed", error: "boom" });
  });

  it("sorts a pending message after a server item of the same instant", () => {
    const at = "2026-10-13T07:13:00-07:00";
    const thread = mergeConversation(server, [pending("p3", at, "thanks")]);
    expect(thread.map((e) => e.text).slice(-2)).toEqual(["The PO is 55821.", "thanks"]);
  });
});

describe("groupByDay", () => {
  it("starts a group when the local day changes", () => {
    const groups = groupByDay(
      [
        { at: "2026-10-13T23:50:00-07:00" },
        { at: "2026-10-14T06:55:00Z" }, // still Oct 13 in Los Angeles
        { at: "2026-10-14T08:00:00-07:00" },
      ],
      LA,
    );
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ["Tue, Oct 13", 2],
      ["Wed, Oct 14", 1],
    ]);
  });
});

describe("emailRecipients", () => {
  const outbox: OutboxItem[] = [
    {
      outbox_id: "out_4",
      idempotency_key: "timer:t2:0",
      run_id: "run_4",
      load_id: "481207",
      kind: "email",
      payload: { to_contact_ids: ["dsp_4410"], subject: "ETA", body: "Your driver does not answer.", purpose: "escalation" },
      created_at: "2026-10-13T08:01:00-07:00",
      status: "sent",
      attempts: 1,
      last_error: null,
    },
  ];

  it("reads the recipients of an agent email from the outbox", () => {
    expect(emailRecipients({ direction: "out", text: "Your driver does not answer." }, outbox)).toEqual(["dsp_4410"]);
  });

  it("returns none for inbound mail or an unknown body", () => {
    expect(emailRecipients({ direction: "in", text: "Your driver does not answer." }, outbox)).toEqual([]);
    expect(emailRecipients({ direction: "out", text: "other" }, outbox)).toEqual([]);
  });
});
