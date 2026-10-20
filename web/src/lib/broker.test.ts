import { describe, expect, it } from "vitest";

import type { Broker } from "../api";
import { newBroker } from "../mocks/brokers";
import {
  brokerProblems,
  followUpIntentOf,
  isSnakeId,
  linesToList,
  newIntent,
  newObligation,
  removeAt,
  replaceAt,
  rolloutSummary,
  sameJson,
} from "./broker";

const acme = (): Broker => newBroker("acme", "Acme Freight");

describe("ids and lists", () => {
  it("takes snake_case ids that start with a letter", () => {
    expect(["acme", "detention_request", "pod2"].every(isSnakeId)).toBe(true);
    expect(["Acme", "2pod", "detention-request", "", "pod "].some(isSnakeId)).toBe(false);
  });

  it("defaults the follow-up intent to <kind>_follow_up_due", () => {
    expect(followUpIntentOf({ kind: "pod", follow_up_intent: "" })).toBe("pod_follow_up_due");
    expect(followUpIntentOf({ kind: "eta_request", follow_up_intent: "eta_follow_up_due" })).toBe("eta_follow_up_due");
  });

  it("replaces and removes by index without touching the rest", () => {
    const list = [{ a: 1 }, { a: 2 }, { a: 3 }];
    expect(replaceAt(list, 1, { a: 9 })).toEqual([{ a: 1 }, { a: 9 }, { a: 3 }]);
    expect(removeAt(list, 0)).toEqual([{ a: 2 }, { a: 3 }]);
    expect(list).toEqual([{ a: 1 }, { a: 2 }, { a: 3 }]);
  });

  it("reads one example per line, blanks dropped", () => {
    expect(linesToList(" detention started at 10:15 \n\n waiting 3 hours ")).toEqual(["detention started at 10:15", "waiting 3 hours"]);
  });

  it("compares JSON values structurally", () => {
    expect(sameJson({ a: [1, 2] }, { a: [1, 2] })).toBe(true);
    expect(sameJson({ a: [1, 2] }, { a: [2, 1] })).toBe(false);
  });
});

describe("brokerProblems", () => {
  it("passes the standard catalog", () => {
    expect(brokerProblems(acme())).toEqual([]);
  });

  it("names bad and duplicate intent ids", () => {
    const broker = acme();
    broker.intents = [...broker.intents, newIntent("Detention"), newIntent("question")];
    expect(brokerProblems(broker)).toEqual([
      'Intent id "Detention" must be snake_case, like detention_request.',
      "Intent question is listed twice.",
    ]);
  });

  it("asks for the follow-up intent of a new obligation type, as a system intent", () => {
    const broker = acme();
    broker.obligations = [...broker.obligations, newObligation("pod")];
    expect(brokerProblems(broker)).toEqual(["pod: its follow-up intent pod_follow_up_due is not in the catalog."]);
    broker.intents = [...broker.intents, newIntent("pod_follow_up_due")];
    expect(brokerProblems(broker)).toEqual(["pod: its follow-up intent pod_follow_up_due must be a system intent."]);
    broker.intents = broker.intents.map((i) => (i.intent_id === "pod_follow_up_due" ? { ...i, system: true } : i));
    expect(brokerProblems(broker)).toEqual([]);
  });

  it("checks requirements against the document types and the obligation kinds", () => {
    const broker = acme();
    broker.documents = [
      { type_id: "pod", at_stage: "at_delivery", within_minutes: 120, opens_obligation: "pod" },
      { type_id: "lumper", at_stage: "at_delivery", within_minutes: 0, opens_obligation: null },
    ];
    expect(brokerProblems(broker)).toEqual([
      "Requirement 1: it opens pod, which is not an obligation type of this broker.",
      "Requirement 2: unknown document type lumper.",
      'Requirement 2: "within" must be at least 1 minute.',
    ]);
  });

  it("checks the loop budget, where 0 steps is allowed (the single-plan mode)", () => {
    const broker = acme();
    broker.limits = { ...broker.limits, max_steps: 0 };
    expect(brokerProblems(broker)).toEqual([]);
    broker.limits = { ...broker.limits, max_steps: -1, max_cost_usd: 0 };
    expect(brokerProblems(broker)).toEqual([
      "Max steps cannot be negative (0 is the single-plan mode).",
      "The cost budget per run must be above $0.",
    ]);
  });
});

describe("rolloutSummary", () => {
  it("says which version the loads run", () => {
    expect(rolloutSummary(2, { percent: 100, previous_version: 1 })).toBe("Every load runs version 2.");
    expect(rolloutSummary(3, { percent: 20, previous_version: 2 })).toBe("20% of loads run version 3, 80% stay on version 2.");
    expect(rolloutSummary(3, { percent: 0, previous_version: 2 })).toBe("Rolled back: every load runs version 2.");
    expect(rolloutSummary(1, { percent: 40, previous_version: null })).toBe(
      "Every load runs version 1: there is no previous version to split with.",
    );
  });
});
