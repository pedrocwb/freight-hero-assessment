import { describe, expect, it } from "vitest";

import { clip, compactArgs, rejectionRule, stepVerdict, tallyTrajectory } from "./trajectory";

describe("compactArgs", () => {
  it("writes key=value in the model's order, quoting strings with spaces", () => {
    expect(compactArgs({ contact_id: "drv_20931", text: "What time will you arrive?", purpose: "eta_request" })).toBe(
      'contact_id=drv_20931, text="What time will you arrive?", purpose=eta_request',
    );
  });

  it("puts lists in brackets and shows numbers, booleans and null as they are", () => {
    expect(compactArgs({ to_contact_ids: ["receiving@valleydc.example", "dsp_4410"], due_in_minutes: 20, urgent: false, subject: null })).toBe(
      "to_contact_ids=[receiving@valleydc.example, dsp_4410], due_in_minutes=20, urgent=false, subject=null",
    );
  });

  it("clips a long string and says when there is nothing", () => {
    expect(compactArgs({ body: "a".repeat(100) }, 10)).toBe(`body=${"a".repeat(9)}…`);
    expect(compactArgs({})).toBe("no arguments");
    expect(compactArgs(undefined)).toBe("no arguments");
  });
});

describe("clip", () => {
  it("flattens whitespace and cuts with an ellipsis", () => {
    expect(clip("one\n two   three", 40)).toBe("one two three");
    expect(clip("abcdefghij", 5)).toBe("abcd…");
  });
});

describe("the verdicts", () => {
  const steps = [
    { tool: "search_sop", args: {}, result: "3 sections", accepted: null, cost_usd: 0.0001 },
    { tool: "send_sms", args: {}, result: "accepted as action #1", accepted: true, cost_usd: 0.0002 },
    {
      tool: "send_email",
      args: {},
      result: "rejected by recipient_is_contact: receiving@valleydc.example is not a contact",
      accepted: false,
      cost_usd: 0.0002,
    },
    { tool: "send_sms", args: {}, result: "accepted as action #2", accepted: true, cost_usd: 0.0001 },
  ];

  it("reads null as a read, true as accepted, false as rejected", () => {
    expect(steps.map(stepVerdict)).toEqual(["read", "accepted", "rejected", "accepted"]);
  });

  it("names the rule of a rejected call", () => {
    expect(rejectionRule(steps[2]!.result)).toBe("recipient_is_contact");
    expect(rejectionRule("accepted as action #1")).toBeNull();
  });

  it("tallies the calls, the cost and the distinct tools in order", () => {
    const tally = tallyTrajectory(steps);
    expect(tally).toMatchObject({ steps: 4, reads: 1, accepted: 2, rejected: 1, tools: ["search_sop", "send_sms", "send_email"] });
    expect(tally.costUsd).toBeCloseTo(0.0006);
  });
});
