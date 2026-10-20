import clsx from "clsx";
import { useMemo, useState } from "react";

import { api, type EvalMode } from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag, percent, usd } from "../components/ui";
import { useBrokerList } from "../hooks/useBrokers";
import { useResource } from "../hooks/useResource";
import type { EvalSummary } from "../lib/evals";
import { useToasts } from "../state/toasts";

export const EVAL_MODES: { id: EvalMode; label: string; hint: string }[] = [
  { id: "scripted", label: "Scripted", hint: "Each scenario's own model answers; checks the harness and the code, no model." },
  { id: "replay", label: "Replay", hint: "Recorded model answers (cassettes); offline and deterministic." },
  { id: "live", label: "Live", hint: "The real models through OpenRouter, recorded as a LangSmith experiment; it costs money, so it stops at the cost cap." },
];

export const DEFAULT_COST_CAP_USD = 0.1;

export function Evals() {
  const toasts = useToasts();
  const scenarios = useResource("evals:scenarios", () => api.evalScenarios(), []);
  const brokers = useBrokerList();
  const suites = useMemo(() => [...new Set((scenarios.data ?? []).map((s) => s.suite))].sort(), [scenarios.data]);
  const [suite, setSuite] = useState("core");
  const [brokerId, setBrokerId] = useState("");
  const [mode, setMode] = useState<EvalMode>("scripted");
  const [k, setK] = useState(1);
  const [costCap, setCostCap] = useState(DEFAULT_COST_CAP_USD);
  const [running, setRunning] = useState<string | null>(null);
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const [filter, setFilter] = useState("");

  const activeSuite = suites.includes(suite) ? suite : suites[0] ?? suite;
  const list = (scenarios.data ?? []).filter(
    (s) =>
      s.suite === activeSuite &&
      (!filter || `${s.id} ${s.title} ${s.tags.join(" ")}`.toLowerCase().includes(filter.toLowerCase())),
  );
  // Live runs and one broker's config go through the broker's suite endpoint (card EVK).
  const modes = EVAL_MODES.filter((m) => m.id !== "live" || brokerId);
  const activeMode = modes.some((m) => m.id === mode) ? mode : "replay";

  async function run(scenario: string | null) {
    setRunning(scenario ?? `suite ${activeSuite}`);
    try {
      if (brokerId) {
        setSummary(
          await api.runBrokerEvals(brokerId, {
            suite: activeSuite,
            mode: activeMode,
            k,
            cost_cap_usd: costCap,
          }),
        );
      } else {
        setSummary(await api.runEvals({ suite: activeSuite, mode: activeMode === "live" ? "replay" : activeMode, k, scenario }));
      }
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setRunning(null);
    }
  }

  const titles = new Map((scenarios.data ?? []).map((s) => [s.id, s.title]));
  const brokerName = brokers.data?.find((b) => b.broker_id === brokerId)?.name;

  return (
    <>
      <ScreenHead
        title="Evals"
        lede="The scenario suites in evals/scenarios, run by the same harness as the CLI against a fresh simulation each time. Pick a broker to run a suite on its config and published SOP."
      />
      <Panel title="Run">
        <form
          className="filters"
          onSubmit={(e) => {
            e.preventDefault();
            void run(null);
          }}
        >
          <label className="field">
            <span className="field-label">Suite</span>
            <select className="input" value={activeSuite} onChange={(e) => setSuite(e.target.value)} disabled={!suites.length}>
              {(suites.length ? suites : [suite]).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Broker</span>
            <select className="input" value={brokerId} onChange={(e) => setBrokerId(e.target.value)}>
              <option value="">Each scenario's own client</option>
              {(brokers.data ?? []).map((b) => (
                <option key={b.broker_id} value={b.broker_id}>
                  {b.name} ({b.broker_id}, v{b.config_version})
                </option>
              ))}
            </select>
          </label>
          <fieldset className="segmented">
            <legend className="field-label">Model</legend>
            {modes.map((m) => (
              <label key={m.id} className={clsx("segment", activeMode === m.id && "segment--on")} title={m.hint}>
                <input type="radio" name="eval-mode" value={m.id} checked={activeMode === m.id} onChange={() => setMode(m.id)} />
                {m.label}
              </label>
            ))}
          </fieldset>
          <label className="field field--narrow">
            <span className="field-label">Trials (k)</span>
            <input className="input" type="number" min={1} max={10} value={k} onChange={(e) => setK(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
          </label>
          {activeMode === "live" ? (
            <label className="field field--narrow">
              <span className="field-label">Cost cap, $</span>
              <input
                className="input"
                type="number"
                min={0.01}
                max={3}
                step={0.01}
                value={costCap}
                onChange={(e) => setCostCap(Math.max(0.01, Math.min(3, Number(e.target.value) || DEFAULT_COST_CAP_USD)))}
              />
            </label>
          ) : null}
          <button type="submit" className="btn btn--primary" disabled={running !== null || !list.length}>
            {running === `suite ${activeSuite}` ? "Running…" : `Run suite ${activeSuite}${brokerId ? ` for ${brokerId}` : ""}`}
          </button>
        </form>
        {brokers.error ? <InlineError message={brokers.error} onRetry={() => void brokers.reload()} /> : null}
        <p className="hint">
          {EVAL_MODES.find((m) => m.id === activeMode)?.hint} pass^k counts a scenario as passed only when all k trials pass.
          {brokerId ? ` Every scenario runs with ${brokerName ?? brokerId} as the load's broker; a broker run covers the whole suite.` : ""}
        </p>
      </Panel>

      {running || summary ? <EvalResults summary={summary} running={running} titles={titles} /> : null}

      <Panel
        title={`Scenarios, ${list.length}`}
        flush
        actions={
          <label className="search">
            <span className="visually-hidden">Filter scenarios</span>
            <input className="input input--small" type="search" placeholder="Filter by id, title or tag" value={filter} onChange={(e) => setFilter(e.target.value)} />
          </label>
        }
      >
        {scenarios.error ? <InlineError message={scenarios.error} onRetry={() => void scenarios.reload()} /> : null}
        {scenarios.data && list.length === 0 ? (
          <Empty title={filter ? "No scenario matches" : "No scenario found"}>
            {filter ? "Clear the filter to see every scenario." : "Scenarios are YAML files under evals/scenarios/<suite>/."}
          </Empty>
        ) : null}
        {list.length ? (
          <div className="table-wrap">
            <table className="table table--stack">
              <thead>
                <tr>
                  <th scope="col">Scenario</th>
                  <th scope="col">Tags</th>
                  <th scope="col">
                    <span className="visually-hidden">Run</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <span className="cell-title">{s.title}</span>
                      <span className="cell-sub mono">{s.id}</span>
                    </td>
                    <td>
                      <div className="tag-list">
                        {s.tags.map((t) => (
                          <Tag key={t} tone={t.startsWith("sop:") ? "sop" : "muted"}>
                            {t}
                          </Tag>
                        ))}
                      </div>
                    </td>
                    <td className="actions-cell">
                      <button
                        type="button"
                        className="btn btn--small"
                        disabled={running !== null || Boolean(brokerId)}
                        title={brokerId ? "A broker run covers the whole suite" : undefined}
                        onClick={() => void run(s.id)}
                      >
                        {running === s.id ? "Running…" : "Run"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Panel>
    </>
  );
}

/** A suite's results: one row per scenario, failures first. The SOP studio shows it too. */
export function EvalResults({ summary, running, titles }: { summary: EvalSummary | null; running: string | null; titles: Map<string, string> }) {
  const [openId, setOpenId] = useState<string | null>(null);
  if (running && !summary) {
    return (
      <Panel title="Results">
        <p className="muted" aria-busy="true">
          Running {running}. A replay of the whole suite takes a few seconds.
        </p>
      </Panel>
    );
  }
  if (!summary) return null;
  const failed = summary.scenarios.filter((s) => !s.passed).length;
  return (
    <Panel
      title="Results"
      actions={running ? <span className="muted">Running {running}…</span> : null}
      flush
    >
      <div className={clsx("result-line", failed ? "result-line--fail" : "result-line--pass")}>
        <strong>
          {summary.passed} of {summary.total} passed
        </strong>
        <span>{usd(summary.costUsd)} in model calls</span>
        {summary.mode ? <span>mode {summary.mode}</span> : null}
        {summary.k ? <span>k = {summary.k}</span> : null}
        {summary.langsmithUrl ? (
          <a href={summary.langsmithUrl} target="_blank" rel="noreferrer">
            Open the LangSmith experiment
          </a>
        ) : null}
      </div>
      {summary.scenarios.length === 0 ? (
        <div className="panel-pad">
          <p className="muted">The API returned a summary this page does not recognize:</p>
          <pre className="raw">{JSON.stringify(summary.raw, null, 2)}</pre>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table table--stack">
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Result</th>
                <th scope="col" className="num">pass^k</th>
                <th scope="col">Failures</th>
                <th scope="col" className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {[...summary.scenarios]
                .sort((a, b) => Number(a.passed) - Number(b.passed))
                .map((s) => {
                  const count = s.failures.length + (s.error && !s.failures.some((f) => f.message === s.error) ? 1 : 0);
                  const open = openId === s.scenarioId;
                  return (
                    <tr key={s.scenarioId} className={clsx(!s.passed && "row--fail")}>
                      <td>
                        <span className="cell-title">{titles.get(s.scenarioId) ?? s.scenarioId}</span>
                        <span className="cell-sub mono">{s.scenarioId}</span>
                      </td>
                      <td>
                        <span className={clsx("chip", s.passed ? "chip--pass" : "chip--fail")}>{s.passed ? "Pass" : "Fail"}</span>
                      </td>
                      <td className="num">{s.passK === null ? "n/a" : percent(s.passK)}</td>
                      <td>
                        {count === 0 ? (
                          <span className="muted">none</span>
                        ) : (
                          <>
                            <button type="button" className="linklike" aria-expanded={open} onClick={() => setOpenId(open ? null : s.scenarioId)}>
                              {count} failure{count === 1 ? "" : "s"}
                            </button>
                            {open ? (
                              <ul className="failures">
                                {s.error ? <li className="failure">{s.error}</li> : null}
                                {s.failures
                                  .filter((f) => f.message !== s.error)
                                  .map((f, i) => (
                                    <li key={i} className="failure">
                                      {f.step !== null ? <span className="muted">Step {f.step}, </span> : null}
                                      {f.check ? <span className="mono">{f.check}</span> : null}
                                      {f.check ? ": " : ""}
                                      {f.message}
                                    </li>
                                  ))}
                              </ul>
                            ) : null}
                          </>
                        )}
                      </td>
                      <td className="num">{usd(s.costUsd)}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
