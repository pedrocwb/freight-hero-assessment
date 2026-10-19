/**
 * The eval suite summary, normalized.
 *
 * The contract describes `POST /api/evals/run` only as "the suite summary JSON (per
 * scenario: passed, pass^k, failures, cost)", so the page accepts the reasonable spellings
 * of each field and keeps the raw payload to show when nothing matches.
 */

export interface EvalFailure {
  step: number | null;
  check: string;
  message: string;
}

export interface EvalScenarioOutcome {
  scenarioId: string;
  passed: boolean;
  passK: number | null; // 0..1; pass^k over the trials
  trials: number | null;
  failures: EvalFailure[];
  costUsd: number;
  error: string | null;
}

export interface EvalSummary {
  suite: string | null;
  mode: string | null;
  k: number | null;
  total: number;
  passed: number;
  costUsd: number;
  scenarios: EvalScenarioOutcome[];
  raw: unknown;
}

type Obj = Record<string, unknown>;

const isObj = (value: unknown): value is Obj =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function pick(obj: Obj, keys: readonly string[]): unknown {
  for (const key of keys) if (obj[key] !== undefined && obj[key] !== null) return obj[key];
  return undefined;
}

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function failure(value: unknown): EvalFailure {
  if (isObj(value)) {
    return {
      step: num(pick(value, ["step_index", "step"])),
      check: str(pick(value, ["check", "name"])) ?? "",
      message: str(pick(value, ["message", "detail", "reason"])) ?? JSON.stringify(value),
    };
  }
  return { step: null, check: "", message: String(value) };
}

function failuresOf(entry: Obj): EvalFailure[] {
  const direct = pick(entry, ["failures", "errors_list"]);
  if (Array.isArray(direct)) return direct.map(failure);
  const trials = entry.trials;
  if (Array.isArray(trials)) {
    return trials.flatMap((trial) =>
      isObj(trial) && Array.isArray(trial.failures) ? trial.failures.map(failure) : [],
    );
  }
  return [];
}

function scenario(value: unknown, index: number): EvalScenarioOutcome {
  const entry = isObj(value) ? value : {};
  const failures = failuresOf(entry);
  const error = str(entry.error);
  const passK = num(pick(entry, ["pass_k", "pass^k", "pass_hat_k", "pass_power_k"]));
  const passedRaw = pick(entry, ["passed", "pass", "ok"]);
  const statusText = str(entry.status);
  const passed =
    typeof passedRaw === "boolean"
      ? passedRaw
      : statusText
        ? statusText.toLowerCase() === "passed" || statusText.toLowerCase() === "pass"
        : passK !== null
          ? passK >= 1
          : failures.length === 0 && !error;
  const trials = pick(entry, ["trials", "k"]);
  return {
    scenarioId: str(pick(entry, ["scenario_id", "id", "scenario", "name"])) ?? `scenario ${index + 1}`,
    passed,
    passK,
    trials: Array.isArray(trials) ? trials.length : num(trials),
    failures,
    costUsd: num(pick(entry, ["cost_usd", "cost", "total_cost_usd"])) ?? 0,
    error,
  };
}

export function normalizeEvalSummary(raw: unknown): EvalSummary {
  const top: Obj = isObj(raw) ? raw : {};
  const list = Array.isArray(raw)
    ? raw
    : (pick(top, ["scenarios", "results", "outcomes"]) as unknown);
  const scenarios = Array.isArray(list) ? list.map(scenario) : [];
  const summed = scenarios.reduce((sum, s) => sum + s.costUsd, 0);
  // A top-level `passed` may be the suite verdict (a boolean) rather than a count.
  const count = (value: unknown) => (typeof value === "number" ? value : null);
  return {
    suite: str(top.suite),
    mode: str(top.mode),
    k: num(top.k),
    total: count(pick(top, ["total", "count"])) ?? scenarios.length,
    passed:
      count(pick(top, ["passed_count", "passed"])) ?? scenarios.filter((s) => s.passed).length,
    costUsd: num(pick(top, ["cost_usd", "total_cost_usd", "cost"])) ?? summed,
    scenarios,
    raw,
  };
}
