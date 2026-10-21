import { useMemo, useState } from "react";

import { api, modelReady, type Scenario } from "../api";
import { ScenarioChecks } from "../components/ScenarioChecks";
import { InlineError } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { filterScenarios, groupBySuite, runOf, startBlocker } from "../lib/scenarios";
import { loadHref } from "../lib/route";
import { useSim } from "../state/sim";

/**
 * The eval scenarios as a plain list. Run plays one live in a fresh world and opens its
 * load's conversation, where its step checks show as it plays.
 */
export function Scenarios() {
  const sim = useSim();
  const scenarios = useResource("scenarios", () => api.scenarios(), ["scenario"]);
  const [query, setQuery] = useState("");
  const list = scenarios.data ?? [];
  const groups = useMemo(() => groupBySuite(filterScenarios(list, query)), [list, query]);
  const blocker = startBlocker({ modelReady: modelReady(sim.state), playing: sim.playing });

  async function run(scenario: Scenario) {
    const started = await sim.startScenario(scenario.id);
    if (started) window.location.hash = loadHref(started.load_id);
  }

  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">Scenarios</h1>
        <p className="page-lede">Each plays live on the models in a fresh simulation, about a tenth of a cent per run. {blocker ?? ""}</p>
        <label className="page-filter">
          <span className="visually-hidden">Filter scenarios</span>
          <input className="input" type="search" placeholder="Filter by id, title or tag" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
      </header>
      {scenarios.error ? <InlineError message={scenarios.error} onRetry={() => void scenarios.reload()} /> : null}
      {scenarios.data && !groups.length ? <p className="why-sub">{query ? "No scenario matches the filter." : "No scenario under evals/scenarios/ yet."}</p> : null}
      {groups.map((group) => (
        <section key={group.suite} className="suite" aria-labelledby={`suite-${group.suite}`}>
          <h2 className="suite-title" id={`suite-${group.suite}`}>
            {group.suite}
          </h2>
          <ul className="scenario-list">
            {group.scenarios.map((s) => {
              const last = runOf(s.id, sim.scenarioRun);
              return (
                <li key={s.id} className="scenario">
                  <div className="scenario-main">
                    <p className="scenario-title">{s.title}</p>
                    <p className="scenario-sub">
                      <code>{s.id}</code>, {s.steps.length} step{s.steps.length === 1 ? "" : "s"}
                    </p>
                    {last ? (
                      <>
                        <ScenarioChecks run={last} title="Last run" />
                        <a className="scenario-open" href={loadHref(last.load_id)}>
                          Open its conversation
                        </a>
                      </>
                    ) : null}
                  </div>
                  <button type="button" className="button" disabled={Boolean(blocker)} title={blocker ?? `Play ${s.id} live`} onClick={() => void run(s)}>
                    Run
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
