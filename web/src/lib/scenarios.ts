/**
 * The scenarios (`GET /api/scenarios`) and the one that plays or played last
 * (`GET /api/scenarios/run`, the stream's `scenario` event): grouping, filtering, and the
 * steps of a scenario with the run's progress on them.
 */

import type {
  Scenario,
  ScenarioCheck,
  ScenarioRun,
  ScenarioStepKind,
  ScenarioStepStatus,
} from "../api";

export interface SuiteGroup {
  suite: string;
  scenarios: Scenario[];
}

/** Scenarios by suite: "core" first, then by name; each suite's scenarios by id. */
export function groupBySuite(scenarios: readonly Scenario[]): SuiteGroup[] {
  const suites = new Map<string, Scenario[]>();
  for (const scenario of scenarios) {
    const list = suites.get(scenario.suite);
    if (list) list.push(scenario);
    else suites.set(scenario.suite, [scenario]);
  }
  return [...suites.entries()]
    .sort(([a], [b]) => Number(b === "core") - Number(a === "core") || a.localeCompare(b))
    .map(([suite, list]) => ({ suite, scenarios: [...list].sort((a, b) => a.id.localeCompare(b.id)) }));
}

/** Every word of the query in the id, title, tags or description. */
export function filterScenarios(scenarios: readonly Scenario[], query: string): Scenario[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [...scenarios];
  return scenarios.filter((s) => {
    const text = `${s.id} ${s.title} ${s.tags.join(" ")} ${s.description}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
}

export interface StepView {
  index: number;
  at: string;
  note: string;
  /** Null for a step the run reports that the scenario does not list. */
  kind: ScenarioStepKind | null;
  status: ScenarioStepStatus;
  checks: ScenarioCheck[];
  runIds: string[];
}

/** The run, when it is of this scenario. */
export function runOf(scenarioId: string, run: ScenarioRun | null | undefined): ScenarioRun | null {
  return run && run.scenario_id === scenarioId ? run : null;
}

/**
 * The scenario's steps with the run's progress: status, checks and the runs each step
 * caused. Without a run of this scenario every step is pending.
 */
export function stepViews(scenario: Scenario, run: ScenarioRun | null): StepView[] {
  const progress = new Map((runOf(scenario.id, run)?.steps ?? []).map((step) => [step.index, step]));
  const views: StepView[] = scenario.steps.map((step) => {
    const done = progress.get(step.index);
    progress.delete(step.index);
    return {
      index: step.index,
      at: step.at,
      note: step.note,
      kind: step.kind,
      status: done?.status ?? "pending",
      checks: done?.checks ?? [],
      runIds: done?.run_ids ?? [],
    };
  });
  for (const step of progress.values()) {
    views.push({ index: step.index, at: step.at, note: step.note, kind: null, status: step.status, checks: step.checks, runIds: step.run_ids });
  }
  return views.sort((a, b) => a.index - b.index);
}

export interface RunTally {
  steps: number;
  passed: number;
  failed: number;
  checks: number;
  failedChecks: number;
}

export function tallyRun(run: ScenarioRun): RunTally {
  const tally: RunTally = { steps: run.steps.length, passed: 0, failed: 0, checks: 0, failedChecks: 0 };
  for (const step of run.steps) {
    if (step.status === "passed") tally.passed += 1;
    if (step.status === "failed") tally.failed += 1;
    tally.checks += step.checks.length;
    tally.failedChecks += step.checks.filter((c) => !c.passed).length;
  }
  return tally;
}

export function isPlaying(run: ScenarioRun | null | undefined): boolean {
  return run?.status === "running";
}

function progressOf(run: ScenarioRun): number {
  return run.steps.filter((s) => s.status !== "pending" && s.status !== "running").length + (run.finished_at ? 1 : 0);
}

/**
 * The fresher of two reports of the scenario run: a poll's answer can arrive after a
 * stream event it predates. A later start wins; for the same run, the one further along.
 */
export function newerRun(current: ScenarioRun | null, incoming: ScenarioRun | null): ScenarioRun | null {
  if (!incoming) return current;
  if (!current) return incoming;
  const a = Date.parse(current.started_at);
  const b = Date.parse(incoming.started_at);
  if (b !== a || current.scenario_id !== incoming.scenario_id) return b >= a ? incoming : current;
  return progressOf(incoming) >= progressOf(current) ? incoming : current;
}

/** Why a scenario cannot start now, or null when it can. */
export function startBlocker(args: { modelReady: boolean; playing: boolean }): string | null {
  if (!args.modelReady) return "The model is not ready: set OPENROUTER_API_KEY and restart the API.";
  if (args.playing) return "A scenario is playing: wait for it to finish.";
  return null;
}
