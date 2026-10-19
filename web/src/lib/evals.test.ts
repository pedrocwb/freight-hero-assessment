import { describe, expect, it } from "vitest";

import { normalizeEvalSummary } from "./evals";

describe("normalizeEvalSummary", () => {
  it("reads the per-scenario fields under their common spellings", () => {
    const summary = normalizeEvalSummary({
      suite: "core",
      mode: "replay",
      k: 3,
      passed: false,
      results: [
        { id: "a", "pass^k": 1, failures: [], cost: 0.004 },
        {
          scenario_id: "b",
          pass_k: 0.67,
          trials: [
            { failures: [{ step_index: 1, check: "expect.actions[0]", message: "no sms" }] },
            { failures: [] },
          ],
          cost_usd: 0.002,
        },
      ],
    });
    expect(summary).toMatchObject({ suite: "core", mode: "replay", k: 3, total: 2, passed: 1 });
    expect(summary.costUsd).toBeCloseTo(0.006);
    expect(summary.scenarios[0]).toMatchObject({ scenarioId: "a", passed: true, passK: 1 });
    expect(summary.scenarios[1]).toMatchObject({ scenarioId: "b", passed: false, trials: 2 });
    expect(summary.scenarios[1]?.failures).toEqual([
      { step: 1, check: "expect.actions[0]", message: "no sms" },
    ]);
  });

  it("accepts a bare list and string failures, and an explicit verdict wins", () => {
    const summary = normalizeEvalSummary([
      { scenario: "c", passed: false, failures: ["missing cassette"], error: "missing cassette" },
      { scenario: "d", status: "passed" },
    ]);
    expect(summary.passed).toBe(1);
    expect(summary.scenarios[0]?.failures[0]?.message).toBe("missing cassette");
    expect(summary.scenarios[1]?.passed).toBe(true);
  });

  it("keeps an unknown payload to show as is", () => {
    const summary = normalizeEvalSummary({ something: "else" });
    expect(summary.scenarios).toEqual([]);
    expect(summary.raw).toEqual({ something: "else" });
  });
});
