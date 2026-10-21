import { useEffect, useMemo, useRef, useState } from "react";

import { api, runCost } from "../api";
import { DocumentsPanel } from "../components/Documents";
import { FacilityMemoryPanel } from "../components/FacilityMemory";
import { FreePlay } from "../components/FreePlay";
import { LedgerPanel, TimersPanel } from "../components/Ledger";
import { LoadCard } from "../components/LoadCard";
import { NewLoadDialog } from "../components/NewLoadDialog";
import { Timeline } from "../components/Timeline";
import { Empty, InlineError, Panel, RiskChip, ScreenHead, usd } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { stageLabel } from "../lib/load";
import { href } from "../lib/route";
import { buildTimeline } from "../lib/timeline";
import { useSim } from "../state/sim";

/**
 * One load's story: every inbound event and the run it caused, opened on one run at a
 * time (its path on the graph, its tool calls, its answer and its actions), with the
 * ledger, the timers and the documents beside it, and free play to move the story on.
 */
export function LoadInspector({ params }: { params: URLSearchParams }) {
  const sim = useSim();
  const { snapshot, load, selectedLoadId, loads, selectLoad } = sim;
  const [creating, setCreating] = useState(false);
  const wantedLoad = params.get("load");
  const wantedRun = params.get("run");

  // A link names a load: select it once it is in this simulation.
  const applied = useRef<string | null>(null);
  useEffect(() => {
    if (!wantedLoad || applied.current === wantedLoad) return;
    if (loads.some((l) => l.load_id === wantedLoad)) {
      applied.current = wantedLoad;
      selectLoad(wantedLoad);
    }
  }, [wantedLoad, loads, selectLoad]);
  const awaitingScenarioLoad = sim.playing && sim.scenarioRun?.load_id === wantedLoad;
  const missing = wantedLoad && sim.state && !awaitingScenarioLoad && !loads.some((l) => l.load_id === wantedLoad) ? wantedLoad : null;

  const data = load ? snapshot.data : undefined;
  // The runs as `GET /api/loads/{id}/runs` serves them, with the graph nodes each went through.
  const runList = useResource(load ? `runs:${load.load_id}` : null, () => api.runs(load!.load_id), ["snapshot"]);
  const runs = useMemo(() => runList.data ?? data?.runs ?? [], [runList.data, data]);
  const entries = useMemo(() => (data ? buildTimeline({ events: data.events, runs }) : []), [data, runs]);
  const latest = entries.flatMap((e) => e.runs).at(-1)?.run.run_id ?? null;

  // The open run: the one a link names, else the one clicked, else the latest.
  const [choice, setChoice] = useState<{ loadId: string | null; runId: string | null } | null>(null);
  useEffect(() => {
    setChoice(wantedRun ? { loadId: wantedLoad, runId: wantedRun } : null);
  }, [wantedRun, wantedLoad]);
  const chosen = choice && (choice.loadId === null || choice.loadId === selectedLoadId) ? choice.runId : undefined;
  const openRunId = chosen !== undefined && (chosen === null || runs.some((r) => r.run_id === chosen)) ? chosen : latest;

  // Bring a linked run into view once it is drawn.
  const scrolled = useRef<string | null>(null);
  useEffect(() => {
    if (!wantedRun || scrolled.current === wantedRun || !runs.some((r) => r.run_id === wantedRun)) return;
    scrolled.current = wantedRun;
    document.getElementById(`run-card-${wantedRun}`)?.scrollIntoView({ block: "start" });
  }, [wantedRun, runs]);

  const summary = loads.find((l) => l.load_id === selectedLoadId);
  const total = runs.reduce((sum, r) => sum + runCost(r), 0);
  const playingHere = sim.playing && sim.scenarioRun?.load_id === selectedLoadId;

  return (
    <>
      <ScreenHead
        title={load ? `Load ${load.load_id}` : "Load inspector"}
        lede={
          load ? (
            <span className="load-strip">
              <span>{load.broker_name}</span>
              <span className="chip chip--stage">{stageLabel(load.stage)}</span>
              <RiskChip risk={load.delay_risk} />
              <span>
                {runs.length} run{runs.length === 1 ? "" : "s"}, {usd(summary?.cost_usd ?? total)} in model calls
              </span>
            </span>
          ) : (
            "Every event of a load and the run it caused: the path through the graph, each tool call, the answer and the actions."
          )
        }
        actions={
          <button type="button" className="btn" onClick={() => setCreating(true)} disabled={sim.playing}>
            New load
          </button>
        }
      />
      {missing ? (
        <p className="banner banner--warn" role="status">
          Load <span className="mono">{missing}</span> is no longer in the simulation: a reset or another scenario replaced it.
        </p>
      ) : null}
      {playingHere ? (
        <p className="banner banner--info" role="status">
          A scenario is playing on this load; follow its checks on the <a href={href("scenarios")}>Scenarios</a> screen.
        </p>
      ) : null}

      {!selectedLoadId ? (
        <Panel title="No load">
          <Empty
            title="No load in this simulation"
            action={
              <button type="button" className="btn btn--primary" onClick={() => setCreating(true)}>
                New load
              </button>
            }
          >
            Run a scenario from the <a href={href("scenarios")}>Scenarios</a> screen, or create a load from a fixture
            and play it by hand.
          </Empty>
        </Panel>
      ) : (
        <div className="grid grid--inspector">
          <section className="col-main" aria-labelledby="timeline-title">
            <h2 className="visually-hidden" id="timeline-title">
              Timeline
            </h2>
            {snapshot.error && !data ? <InlineError message={snapshot.error} onRetry={() => void snapshot.reload()} /> : null}
            {!data && !snapshot.error ? <p className="muted">{awaitingScenarioLoad ? "The scenario is creating its load…" : "Loading the load…"}</p> : null}
            {data && entries.length === 0 ? (
              <Panel title="Timeline">
                <Empty title="Nothing has happened yet">
                  Send a high delay-risk ping or a message from Free play, then move the clock past the inbound latency.
                </Empty>
              </Panel>
            ) : null}
            {data && entries.length ? (
              <Timeline
                entries={entries}
                snapshot={data}
                openRunId={openRunId}
                onToggle={(runId) => setChoice({ loadId: selectedLoadId, runId: openRunId === runId ? null : runId })}
              />
            ) : null}
          </section>
          <div className="col-side">
            {load ? <FreePlay load={load} /> : null}
            <LedgerPanel snapshot={data} />
            <TimersPanel snapshot={data} />
            <DocumentsPanel />
            <FacilityMemoryPanel />
            {load ? <LoadCard load={load} /> : null}
          </div>
        </div>
      )}
      <NewLoadDialog open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
