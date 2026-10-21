import clsx from "clsx";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { api, ApiError, type DelayRisk, type LoadSnapshot, type RunRecord } from "../api";
import { ChatThread } from "../components/ChatThread";
import { ComposerBar, type Voice } from "../components/ComposerBar";
import { LoadFacts } from "../components/LoadFacts";
import { Modal } from "../components/Modal";
import { NewLoadDialog } from "../components/NewLoadDialog";
import { SaveScenarioDialog } from "../components/SaveScenarioDialog";
import { ScenarioChecks } from "../components/ScenarioChecks";
import { WhyPanel } from "../components/WhyPanel";
import { useResource } from "../hooks/useResource";
import { ALL_TOPICS, invalidate } from "../lib/bus";
import { buildConversation } from "../lib/conversation";
import { currentStop, roleLabel, stageLabel } from "../lib/load";
import { href } from "../lib/route";
import { flowReducer, IDLE } from "../lib/sendFlow";
import { formatTime, formatWindow } from "../lib/time";
import type { SectionInfo } from "../lib/why";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";

const WIDE = "(min-width: 1100px)";

function useWide(): boolean {
  const [wide, setWide] = useState(() => typeof window !== "undefined" && window.matchMedia?.(WIDE).matches);
  useEffect(() => {
    const media = window.matchMedia?.(WIDE);
    if (!media) return undefined;
    const onChange = (e: MediaQueryListEvent) => setWide(e.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return wide;
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/**
 * One load as a conversation: what its people wrote and what the agent answered, with
 * "why" one click away, and a composer that sends as a contact and waits for the agent.
 */
export function Conversation({ params }: { params: URLSearchParams }) {
  const sim = useSim();
  const toasts = useToasts();
  const { snapshot, load, loads, selectedLoadId, selectLoad, timezone } = sim;
  const wantedLoad = params.get("load");
  const wantedRun = params.get("run");
  const wide = useWide();

  // A link names a load: select it once it is in this simulation.
  const applied = useRef<string | null>(null);
  useEffect(() => {
    if (!wantedLoad || applied.current === wantedLoad) return;
    if (loads.some((l) => l.load_id === wantedLoad)) {
      applied.current = wantedLoad;
      selectLoad(wantedLoad);
    }
  }, [wantedLoad, loads, selectLoad]);

  const data: LoadSnapshot | undefined = load ? snapshot.data : undefined;
  const recording = useResource(load ? `recording:${load.load_id}` : null, () => api.recording(load!.load_id), ["snapshot"]);
  const sections = useResource(
    load ? `sops:${load.broker_id}:${load.load_types.join(",")}` : null,
    () => api.sops(load!.broker_id, load!.load_types),
    [],
  );
  const sectionMap = useMemo(
    () => new Map<string, SectionInfo>((sections.data?.sections ?? []).map((s) => [s.section_id, { title: s.title, sources: s.sources }])),
    [sections.data],
  );
  const items = useMemo(() => (data ? buildConversation(data, data.load) : []), [data]);

  const [selected, setSelected] = useState<string | null>(wantedRun);
  useEffect(() => setSelected(wantedRun), [wantedRun, selectedLoadId]);
  const run: RunRecord | undefined = data?.runs.find((r) => r.run_id === selected);
  const step = run ? recording.data?.find((s) => s.run_ids.includes(run.run_id)) ?? null : null;

  const [flow, dispatch] = useReducer(flowReducer, IDLE);
  const [factsOpen, setFactsOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);

  const now = sim.state?.now;
  const playingHere = sim.playing && sim.scenarioRun?.load_id === selectedLoadId;
  const scenarioHere = sim.scenarioRun && sim.scenarioRun.load_id === selectedLoadId ? sim.scenarioRun : null;
  const blocked = !sim.state ? "Waiting for the API…" : sim.playing ? "A scenario is playing: it owns the clock until it ends." : null;

  /** One input, then the runs it caused, then the chat says what came of it. */
  const turn = useCallback(
    async (
      kind: "message" | "file" | "ping" | "timer",
      label: string,
      pending: { from: string; channel: string; body: string; attachments: string[] } | null,
      call: () => Promise<{ runs: RunRecord[]; duplicate?: boolean }>,
    ): Promise<boolean> => {
      if (!load) return false;
      dispatch({ type: "start", turn: kind, label, pending });
      try {
        const result = await call();
        const runs = result.runs.filter((r) => r.load_id === load.load_id);
        const after = await api.getLoad(load.load_id);
        dispatch({ type: "finish", runs, outbox: after.outbox, duplicate: result.duplicate });
        const latest = runs.at(-1);
        if (latest && wide) setSelected(latest.run_id);
        return true;
      } catch (error) {
        dispatch({ type: "fail", error: message(error) });
        return false;
      } finally {
        invalidate(...ALL_TOPICS);
      }
    },
    [load, wide],
  );

  async function send(voice: Voice, text: string, file: File | null): Promise<boolean> {
    if (!load || !now) return false;
    const from = roleLabel(voice.role);
    const channel = voice.channel === "sms" ? "SMS" : voice.channel === "email" ? "Email" : "Slack";
    const pending = { from, channel, body: text, attachments: file ? [file.name] : [] };
    if (file) {
      const upload = { file, channel: voice.channel, senderContactId: voice.contactId, body: text, at: now };
      return turn("file", "The agent is reading the file…", pending, async () => {
        const result = await api.uploadAttachment(load.load_id, upload, sim.latencySeconds, true);
        return { runs: result.runs ?? [] };
      });
    }
    return turn("message", "The agent is thinking…", pending, async () => {
      const result = await api.sendTurn(load.load_id, { channel: voice.channel, sender_contact_id: voice.contactId, body: text }, sim.latencySeconds);
      sim.setState(result.state);
      if (!result.accepted) throw new Error(result.reason ?? "The message was not accepted.");
      return result;
    });
  }

  function ping(risk: DelayRisk) {
    if (!load) return;
    void turn("ping", `Ping sent, ${risk} risk. Waiting for the agent…`, null, async () => {
      const result = await api.pingTurn(load.load_id, risk, sim.latencySeconds);
      sim.setState(result.state);
      if (!result.accepted) throw new Error(result.reason ?? "The ping was not accepted.");
      return result;
    });
  }

  function skip() {
    const next = sim.state?.next_timer_due_at;
    if (!next) return;
    void turn("timer", `Moving the clock to ${formatTime(next, timezone)}…`, null, async () => {
      const result = await api.advance({ to: next });
      sim.setState(result.state);
      return result;
    });
  }

  async function reset() {
    setResetOpen(false);
    const state = await sim.perform(() => api.resetSim({}));
    if (state) {
      sim.setState(state);
      dispatch({ type: "dismiss" });
      setSelected(null);
      toasts.info("The simulation was reset: every load, message and run is gone.");
    }
  }

  async function quickCreate() {
    const fixtures = await sim.perform(() => api.fixtures());
    const first = fixtures?.[0];
    if (!first) return;
    const summary = await sim.perform(() => api.createLoad(first.metadata));
    if (summary) sim.addLoad(summary);
  }

  if (!selectedLoadId) {
    return (
      <section className="start">
        <h1 className="start-title">Start with a load</h1>
        <p className="start-text">Create the assignment's load, then write to the agent as its driver, dispatcher or broker.</p>
        <div className="start-actions">
          <button type="button" className="button button--primary" onClick={() => void quickCreate()} disabled={Boolean(blocked) || sim.busy}>
            Create load 481207
          </button>
          <button type="button" className="button" onClick={() => setCreating(true)} disabled={Boolean(blocked)}>
            Choose a fixture
          </button>
          <a className="button button--quiet" href={href("scenarios")}>
            Or run a scenario
          </a>
        </div>
        <NewLoadDialog open={creating} onClose={() => setCreating(false)} />
      </section>
    );
  }

  const stop = load ? currentStop(load) : undefined;
  const appointment = stop
    ? stop.appointment.kind === "window"
      ? formatWindow(stop.appointment.start, stop.appointment.end, stop.timezone)
      : formatTime(stop.appointment.at, stop.timezone)
    : null;
  const caps = Object.fromEntries(Object.entries(recording.data?.at(-1)?.obligations ?? {}).map(([k, o]) => [k, o.max_attempts]));
  const panelOpen = Boolean(run && load);

  return (
    <div className={clsx("chat-screen", panelOpen && "chat-screen--why")}>
      <header className="load-head">
        <div className="load-line">
          {loads.length > 1 ? (
            <label className="load-pick">
              <span className="visually-hidden">Load</span>
              <select className="select select--load" value={selectedLoadId} onChange={(e) => selectLoad(e.target.value)}>
                {loads.map((l) => (
                  <option key={l.load_id} value={l.load_id}>
                    {l.load_id}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <h1 className="load-id">{selectedLoadId}</h1>
          )}
          {load ? (
            <p className="load-facts">
              <span>{load.broker_name}</span>
              <span>{stageLabel(load.stage)}</span>
              <span className={clsx("risk", `risk--${load.delay_risk}`)}>{load.delay_risk} risk</span>
              {stop ? (
                <span className="load-next">
                  Next: {stop.name}, {appointment}
                </span>
              ) : null}
            </p>
          ) : null}
        </div>
        <div className="load-actions">
          <button type="button" className="button button--quiet" onClick={() => setFactsOpen(true)} disabled={!data}>
            Load facts
          </button>
          <button type="button" className="button button--quiet" onClick={() => setSaveOpen(true)} disabled={!data || !recording.data?.length || sim.playing}>
            Save as scenario
          </button>
          <button type="button" className="button button--quiet" onClick={() => setCreating(true)} disabled={Boolean(blocked)}>
            New load
          </button>
          <button type="button" className="button button--quiet" onClick={() => setResetOpen(true)} disabled={Boolean(blocked)}>
            Reset
          </button>
        </div>
      </header>

      <div className="chat-body">
        <section className="chat-column" aria-label="Conversation">
          {scenarioHere ? <ScenarioChecks run={scenarioHere} /> : null}
          {snapshot.error && !data ? <p className="banner banner--alert">{snapshot.error}</p> : null}
          {!data && !snapshot.error ? <p className="thread-empty">{playingHere ? "The scenario is creating its load…" : "Loading the conversation…"}</p> : null}
          {data ? (
            <ChatThread
              items={items}
              timezone={timezone}
              selectedRunId={selected}
              onSelect={(id) => setSelected(selected === id ? null : id)}
              flow={flow}
              emptyHint="Nothing yet. Send a high-risk ping to wake the agent, or write as the driver."
            />
          ) : null}
          {flow.phase === "done" || flow.phase === "failed" ? (
            <p className={clsx("turn-result", flow.phase === "failed" && "turn-result--failed")} role="status">
              {flow.phase === "failed" ? flow.error : flow.summary}
              {flow.phase === "done" && flow.runIds.length ? (
                <button type="button" className="link-button" onClick={() => setSelected(flow.runIds.at(-1)!)}>
                  Why?
                </button>
              ) : null}
            </p>
          ) : null}
          {load ? (
            <ComposerBar
              load={load}
              disabled={blocked}
              waiting={flow.phase === "waiting"}
              nextTimer={sim.state?.next_timer_due_at ?? null}
              timezone={timezone}
              onSend={send}
              onPing={ping}
              onSkip={skip}
            />
          ) : null}
        </section>
        {run && load ? (
          <WhyPanel
            run={run}
            event={data?.events.find((e) => e.event_id === run.event_id) ?? null}
            load={load}
            sections={sectionMap}
            step={step}
            timezone={timezone}
            onClose={() => setSelected(null)}
          />
        ) : null}
      </div>

      {data ? <LoadFacts open={factsOpen} onClose={() => setFactsOpen(false)} snapshot={data} caps={caps} timezone={timezone} now={now} /> : null}
      {data ? <SaveScenarioDialog open={saveOpen} onClose={() => setSaveOpen(false)} snapshot={data} recording={recording.data ?? []} timezone={timezone} now={now} /> : null}
      <NewLoadDialog open={creating} onClose={() => setCreating(false)} />
      <Modal open={resetOpen} title="Reset the simulation" submitLabel="Reset" onClose={() => setResetOpen(false)} onSubmit={() => void reset()}>
        <p className="why-sub">Every load, message, run and timer is dropped, and the clock goes back to 06:55 on the assignment's day.</p>
      </Modal>
    </div>
  );
}
