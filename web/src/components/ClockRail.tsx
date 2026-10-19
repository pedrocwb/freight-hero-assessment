import { useState } from "react";

import { api, type AdvanceBy, type AdvanceResult, type RunRecord } from "../api";
import { currentStop } from "../lib/load";
import { clockTime, formatDay, formatRelative, formatTime, minutesBetween, zoneAbbreviation } from "../lib/time";
import { useSim } from "../state/sim";
import { Modal } from "./Modal";

const STEPS = [1, 5, 30] as const;
const DEFAULT_START = "2026-10-13T06:55:00-07:00";

function describeRuns(runs: RunRecord[]): string {
  if (!runs.length) return "no run";
  const counts = new Map<string, number>();
  for (const run of runs) counts.set(run.status, (counts.get(run.status) ?? 0) + 1);
  const statuses = [...counts].map(([status, n]) => (n > 1 ? `${n} ${status}` : status)).join(", ");
  return `${runs.length} run${runs.length > 1 ? "s" : ""} (${statuses})`;
}

/**
 * The simulation clock, shaped like a highway guide sign, and the controls that move it.
 * It sits beside every screen so time can move while a role is being played.
 */
export function ClockRail() {
  const sim = useSim();
  const { state, timezone, load } = sim;
  const [last, setLast] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [start, setStart] = useState(DEFAULT_START);
  const [mode, setMode] = useState<"" | "replay" | "live">("");

  const stop = load ? currentStop(load) : undefined;
  const now = state?.now;
  const next = state?.next_timer_due_at ?? null;
  const untilNext = now && next ? minutesBetween(now, next) : null;

  async function move(by: AdvanceBy | "tick", label: string) {
    const result = await sim.perform<AdvanceResult>(() => (by === "tick" ? api.tick() : api.advance(by)));
    if (!result) return;
    sim.setState(result.state);
    setLast(`${label}: now ${formatTime(result.state.now, timezone)}, ${describeRuns(result.runs)}`);
  }

  async function reset() {
    setResetOpen(false);
    const result = await sim.perform(() => api.resetSim({ start: start.trim(), llm_mode: mode || null }));
    if (result) {
      sim.setState(result);
      setLast(`Reset to ${formatTime(result.now, timezone)}`);
    }
  }

  return (
    <div className="rail-clock">
      <div className="sign" role="group" aria-label="Simulation clock">
        <div className="sign-inner">
          <p className="sign-place">{stop ? `${stop.name}, ${stop.address}` : "Simulation clock"}</p>
          <p className="sign-time">
            <time dateTime={now}>{now ? clockTime(now, timezone) : "--:--"}</time>
            <span className="sign-zone">{now ? zoneAbbreviation(now, timezone) : ""}</span>
          </p>
          <p className="sign-day">{now ? formatDay(now, timezone) : "Waiting for the API"}</p>
        </div>
      </div>

      <p className="next-timer">
        {next ? (
          <>
            Next timer <strong>{formatTime(next, timezone)}</strong>
            {untilNext !== null ? <span className="muted">, {formatRelative(untilNext)}</span> : null}
          </>
        ) : (
          <span className="muted">No timer pending</span>
        )}
      </p>

      <div className="clock-controls" role="group" aria-label="Move the clock">
        {STEPS.map((minutes) => (
          <button
            key={minutes}
            type="button"
            className="btn"
            disabled={!state || sim.busy}
            onClick={() => void move({ minutes }, `+${minutes} min`)}
          >
            +{minutes} min
          </button>
        ))}
        <button
          type="button"
          className="btn btn--primary clock-next"
          disabled={!next || sim.busy}
          onClick={() => next && void move({ to: next }, "To next timer")}
          title={next ? `Advance to ${formatTime(next, timezone)} and fire the timer` : "No timer pending"}
        >
          To next timer
        </button>
        <button type="button" className="btn" disabled={!state || sim.busy} onClick={() => void move("tick", "Tick")}>
          Tick
        </button>
        <button type="button" className="btn btn--quiet" disabled={sim.busy} onClick={() => setResetOpen(true)}>
          Reset
        </button>
      </div>

      <p className="clock-last" aria-live="polite">
        {sim.busy ? "Working…" : last ?? `Queue: ${state?.queue_size ?? 0} event${state?.queue_size === 1 ? "" : "s"}`}
      </p>

      <Modal
        open={resetOpen}
        title="Reset the simulation"
        submitLabel="Reset simulation"
        onClose={() => setResetOpen(false)}
        onSubmit={() => void reset()}
        submitDisabled={!start.trim()}
      >
        <p className="hint">
          Every load, message, run and timer is dropped and the clock restarts. Fixtures stay
          available to create loads again.
        </p>
        <label className="field">
          <span className="field-label">Start time (ISO 8601 with offset)</span>
          <input className="input mono" value={start} onChange={(e) => setStart(e.target.value)} spellCheck={false} />
        </label>
        <label className="field">
          <span className="field-label">Model mode</span>
          <select className="input" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
            <option value="">Keep the server setting ({state?.llm_mode ?? "replay"})</option>
            <option value="replay">Replay (recorded answers, offline)</option>
            <option value="live">Live (needs OPENROUTER_API_KEY)</option>
          </select>
        </label>
      </Modal>
    </div>
  );
}
