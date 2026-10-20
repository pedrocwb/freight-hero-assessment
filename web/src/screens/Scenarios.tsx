import clsx from "clsx";
import { useMemo, useState } from "react";

import { api, langsmithProject, modelReady, runCost, type Scenario, type ScenarioRun, type ScenarioRunStatus, type ScenarioStepStatus } from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag, usd } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { href, inspectorHref } from "../lib/route";
import { filterScenarios, groupBySuite, runOf, startBlocker, stepViews, tallyRun, type StepView } from "../lib/scenarios";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";

const RUN_STATUS: Record<ScenarioRunStatus, { label: string; chip: string }> = {
  running: { label: "Playing", chip: "chip--timer-pending" },
  passed: { label: "Passed", chip: "chip--pass" },
  failed: { label: "Failed", chip: "chip--fail" },
  error: { label: "Error", chip: "chip--fail" },
};

const STEP_STATUS: Record<ScenarioStepStatus, { label: string; chip: string }> = {
  pending: { label: "Pending", chip: "chip--skipped" },
  running: { label: "Playing", chip: "chip--timer-pending" },
  passed: { label: "Passed", chip: "chip--pass" },
  failed: { label: "Failed", chip: "chip--fail" },
};

const KIND_LABEL = { message: "Message", ping: "Ping", attachment: "Document", wait: "Wait" } as const;

/**
 * The eval scenarios, by suite. "Run" plays one live in a reset world; its steps show
 * their checks as they pass or fail, and link to the runs they caused in the inspector.
 */
export function Scenarios({ params }: { params: URLSearchParams }) {
  const sim = useSim();
  const scenarios = useResource("scenarios", () => api.scenarios(), []);
  const [query, setQuery] = useState("");
  const list = scenarios.data ?? [];
  const groups = useMemo(() => groupBySuite(filterScenarios(list, query)), [list, query]);
  const wanted = params.get("id") ?? sim.scenarioRun?.scenario_id ?? null;
  const selected = list.find((s) => s.id === wanted) ?? groups[0]?.scenarios[0];
  const project = langsmithProject(sim.state);

  return (
    <>
      <ScreenHead
        title="Scenarios"
        lede="The eval scenarios, played live on OpenRouter in a fresh world: each step's checks as it plays, the spend so far, and the runs it caused in the load inspector. LangSmith keeps the traces and the experiments."
        actions={
          project ? (
            <a className="btn" href={project} target="_blank" rel="noreferrer">
              Open the LangSmith project
            </a>
          ) : null
        }
      />
      {sim.state && !modelReady(sim.state) ? (
        <p className="banner banner--warn" role="status">
          The model is not ready: <span className="mono">OPENROUTER_API_KEY</span> is unset, so no scenario can run. Set it
          and restart the API.
        </p>
      ) : null}
      <div className="grid grid--scenarios">
        <Panel
          title={`Scenarios${scenarios.data ? `, ${list.length}` : ""}`}
          flush
          className="scenario-catalog"
          actions={
            <label className="search">
              <span className="visually-hidden">Filter scenarios</span>
              <input
                className="input input--small"
                type="search"
                placeholder="Filter by id, title or tag"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          }
        >
          {scenarios.error ? <InlineError message={scenarios.error} onRetry={() => void scenarios.reload()} /> : null}
          {scenarios.data && groups.length === 0 ? (
            <Empty title={query ? "No scenario matches" : "No scenario found"}>
              {query ? "Clear the filter to see every scenario." : "Scenarios are YAML files under evals/scenarios/<suite>/."}
            </Empty>
          ) : null}
          {groups.map((group) => (
            <section key={group.suite} className="suite" aria-label={`Suite ${group.suite}`}>
              <h3 className="suite-title">
                {group.suite} <span className="muted">{group.scenarios.length}</span>
              </h3>
              <ul className="catalog">
                {group.scenarios.map((s) => (
                  <ScenarioItem key={s.id} scenario={s} selected={s.id === selected?.id} run={runOf(s.id, sim.scenarioRun)} />
                ))}
              </ul>
            </section>
          ))}
        </Panel>
        <div className="col-main">{selected ? <ScenarioDetail scenario={selected} /> : null}</div>
      </div>
    </>
  );
}

function ScenarioItem({ scenario, selected, run }: { scenario: Scenario; selected: boolean; run: ScenarioRun | null }) {
  return (
    <li>
      <a
        href={href("scenarios", { id: scenario.id })}
        className={clsx("catalog-item", selected && "catalog-item--on")}
        aria-current={selected ? "true" : undefined}
      >
        <span className="catalog-title">
          {scenario.title}
          {run ? <span className={clsx("chip", "catalog-chip", RUN_STATUS[run.status].chip)}>{RUN_STATUS[run.status].label}</span> : null}
        </span>
        <span className="mono catalog-id">{scenario.id}</span>
      </a>
    </li>
  );
}

function ScenarioDetail({ scenario }: { scenario: Scenario }) {
  const sim = useSim();
  const toasts = useToasts();
  const [starting, setStarting] = useState(false);
  const run = runOf(scenario.id, sim.scenarioRun);
  const steps = stepViews(scenario, run);
  const blocker = startBlocker({ modelReady: modelReady(sim.state), playing: sim.playing });
  const other = sim.playing && sim.scenarioRun && !run ? sim.scenarioRun : null;

  async function start() {
    setStarting(true);
    const started = await sim.startScenario(scenario.id);
    setStarting(false);
    if (started) toasts.info(`Playing ${scenario.id} on load ${started.load_id}.`);
  }

  return (
    <Panel
      title={scenario.title}
      actions={
        <button type="button" className="btn btn--primary" disabled={Boolean(blocker) || starting} title={blocker ?? undefined} onClick={() => void start()}>
          {starting ? "Starting…" : run && run.status !== "running" ? "Run again" : "Run"}
        </button>
      }
    >
      <div className="stack">
        <p className="scenario-desc">{scenario.description || <span className="muted">No description.</span>}</p>
        <div className="tag-list">
          <Tag tone="muted">suite {scenario.suite}</Tag>
          {scenario.tags.map((t) => (
            <Tag key={t} tone={t.startsWith("sop:") ? "sop" : "muted"}>
              {t}
            </Tag>
          ))}
          <span className="mono muted scenario-id">{scenario.id}</span>
        </div>
        <p className="hint">
          {blocker ??
            "Run resets the simulation and plays every step live: the models are called through OpenRouter, so each run costs money."}
        </p>
        {other ? (
          <p className="banner banner--info" role="status">
            <a href={href("scenarios", { id: other.scenario_id })}>{other.scenario_id}</a> is playing now.
          </p>
        ) : null}
        {run ? <RunStatusLine run={run} /> : null}
      </div>
      <h3 className="subhead">Steps</h3>
      <ol className="steps">
        {steps.map((step) => (
          <StepItem key={step.index} step={step} loadId={run?.load_id ?? null} />
        ))}
      </ol>
    </Panel>
  );
}

function RunStatusLine({ run }: { run: ScenarioRun }) {
  const tally = tallyRun(run);
  const status = RUN_STATUS[run.status];
  return (
    <div className={clsx("result-line", run.status === "passed" && "result-line--pass", (run.status === "failed" || run.status === "error") && "result-line--fail")}>
      <span className={clsx("chip", status.chip)}>{status.label}</span>
      <strong>
        {tally.passed} of {run.steps.length} steps passed
        {tally.failedChecks ? `, ${tally.failedChecks} check${tally.failedChecks === 1 ? "" : "s"} failed` : ""}
      </strong>
      <span>
        {usd(run.cost_usd)} {run.status === "running" ? "so far" : "in model calls"}
      </span>
      <a href={inspectorHref(run.load_id)}>Load {run.load_id} in the inspector</a>
      {run.error ? <span className="result-error">{run.error}</span> : null}
    </div>
  );
}

function StepItem({ step, loadId }: { step: StepView; loadId: string | null }) {
  const { snapshot } = useSim();
  const status = STEP_STATUS[step.status];
  // Each run's cost, when the inspector's load is the scenario's.
  const runs = snapshot.data?.load.load_id === loadId ? snapshot.data?.runs ?? [] : [];
  const costs = step.runIds.map((id) => runs.find((r) => r.run_id === id)).filter((r) => r !== undefined);
  return (
    <li className={clsx("step", `step--${step.status}`)}>
      <span className="step-time mono">{step.at}</span>
      <div className="step-body">
        <p className="step-head">
          {step.kind ? <Tag tone="muted">{KIND_LABEL[step.kind]}</Tag> : null}
          <span className="step-note">{step.note}</span>
          <span className={clsx("chip", status.chip)}>{status.label}</span>
        </p>
        {step.checks.length ? (
          <ul className="checks">
            {step.checks.map((check, i) => (
              <li key={`${check.name}-${i}`} className={clsx("check", check.passed ? "check--pass" : "check--fail")}>
                <span className="check-mark" aria-hidden="true">
                  {check.passed ? "✓" : "✗"}
                </span>
                <span className="visually-hidden">{check.passed ? "Passed" : "Failed"}: </span>
                <span className="mono">{check.name}</span>
                {check.detail ? <span className="check-detail">{check.detail}</span> : null}
              </li>
            ))}
          </ul>
        ) : null}
        {step.runIds.length && loadId ? (
          <p className="step-runs">
            {step.runIds.map((id, i) => (
              <a key={id} href={inspectorHref(loadId, id)} className="step-run">
                {step.runIds.length === 1 ? "Inspect the run" : `Inspect run ${i + 1}`}
              </a>
            ))}
            {costs.length ? <span className="muted">{usd(costs.reduce((sum, r) => sum + runCost(r), 0))}</span> : null}
          </p>
        ) : null}
      </div>
    </li>
  );
}
