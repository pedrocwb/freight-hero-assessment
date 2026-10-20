import { describe, expect, it } from "vitest";

import type { Scenario } from "../api";
import {
  filterScenarios,
  groupBySuite,
  isPlaying,
  newerRun,
  runOf,
  startBlocker,
  stepViews,
  tallyRun,
} from "./scenarios";
import { SCENARIO, scenarioRun } from "./testFixtures";

const scenario = (id: string, suite: string, over: Partial<Scenario> = {}): Scenario => ({
  id,
  title: id,
  suite,
  tags: [],
  description: "",
  steps: [],
  ...over,
});

describe("groupBySuite", () => {
  it("puts core first, then the other suites by name, each sorted by id", () => {
    const groups = groupBySuite([scenario("p1", "docs"), scenario("n1", "agent"), scenario("c1", "core"), scenario("a1", "core")]);
    expect(groups.map((g) => [g.suite, g.scenarios.map((s) => s.id)])).toEqual([
      ["core", ["a1", "c1"]],
      ["agent", ["n1"]],
      ["docs", ["p1"]],
    ]);
  });
});

describe("filterScenarios", () => {
  const list = [SCENARIO, scenario("p1_pod_clean", "docs", { title: "A clean POD", tags: ["documents"], description: "The POD arrives signed." })];

  it("matches every word in the id, title, tags or description", () => {
    expect(filterScenarios(list, "pod signed").map((s) => s.id)).toEqual(["p1_pod_clean"]);
    expect(filterScenarios(list, "sop:delay_risk_high").map((s) => s.id)).toEqual([SCENARIO.id]);
    expect(filterScenarios(list, "  ")).toHaveLength(2);
    expect(filterScenarios(list, "pod nothing")).toEqual([]);
  });
});

describe("stepViews", () => {
  it("shows every step pending without a run of this scenario", () => {
    expect(stepViews(SCENARIO, null).map((s) => s.status)).toEqual(["pending", "pending", "pending", "pending"]);
    const other = scenarioRun({ scenario_id: "another" });
    expect(stepViews(SCENARIO, other).every((s) => s.status === "pending" && s.runIds.length === 0)).toBe(true);
  });

  it("puts the run's status, checks and runs on each step", () => {
    const views = stepViews(SCENARIO, scenarioRun());
    expect(views.map((s) => s.status)).toEqual(["passed", "failed", "running", "pending"]);
    expect(views[1]?.checks.filter((c) => !c.passed).map((c) => c.detail)).toEqual(["an SMS with purpose eta_request was sent"]);
    expect(views[0]?.runIds).toEqual(["run_1"]);
    expect(views[0]?.kind).toBe("ping");
    expect(views[3]?.kind).toBe("wait");
  });

  it("keeps a step the run reports that the scenario does not list", () => {
    const run = scenarioRun();
    run.steps.push({ index: 7, at: "09:00", note: "extra", status: "passed", checks: [], run_ids: [] });
    const views = stepViews(SCENARIO, run);
    expect(views.at(-1)).toMatchObject({ index: 7, kind: null, status: "passed" });
  });
});

describe("the run's state", () => {
  it("belongs to its scenario only", () => {
    expect(runOf(SCENARIO.id, scenarioRun())).not.toBeNull();
    expect(runOf("another", scenarioRun())).toBeNull();
    expect(runOf(SCENARIO.id, null)).toBeNull();
  });

  it("tallies steps and checks", () => {
    expect(tallyRun(scenarioRun())).toEqual({ steps: 3, passed: 1, failed: 1, checks: 3, failedChecks: 1 });
  });

  it("is playing only while running", () => {
    expect(isPlaying(scenarioRun())).toBe(true);
    expect(isPlaying(scenarioRun({ status: "failed", finished_at: "2026-10-20T18:01:00Z" }))).toBe(false);
    expect(isPlaying(null)).toBe(false);
  });
});

describe("newerRun", () => {
  const early = scenarioRun();
  const later = scenarioRun({ status: "passed", finished_at: "2026-10-20T18:02:00Z" });

  it("keeps the run further along when a stale poll arrives after an event", () => {
    expect(newerRun(later, early)).toBe(later);
    expect(newerRun(early, later)).toBe(later);
  });

  it("takes a run that started later, and any run over none", () => {
    const next = scenarioRun({ scenario_id: "b", started_at: "2026-10-20T19:00:00Z", steps: [] });
    expect(newerRun(later, next)).toBe(next);
    expect(newerRun(next, later)).toBe(next);
    expect(newerRun(null, early)).toBe(early);
    expect(newerRun(early, null)).toBe(early);
  });
});

describe("startBlocker", () => {
  it("says why a run cannot start", () => {
    expect(startBlocker({ modelReady: false, playing: false })).toMatch(/OPENROUTER_API_KEY/);
    expect(startBlocker({ modelReady: true, playing: true })).toMatch(/playing/);
    expect(startBlocker({ modelReady: true, playing: false })).toBeNull();
  });
});
