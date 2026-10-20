import clsx from "clsx";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type DelayRisk, type Fixture, type Load, type LoadMetadata } from "../api";
import { Modal } from "../components/Modal";
import { Empty, InlineError, Panel, RiskChip, ScreenHead, usd } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { currentStop, roleLabel, stageLabel } from "../lib/load";
import { formatDateTime, formatStamp, formatWindow } from "../lib/time";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";

const RISKS: DelayRisk[] = ["low", "medium", "high"];
// Stockton, CA: the pickup of the assignment's load. The API only needs plausible numbers.
const DEFAULT_POSITION = { lat: 37.9577, lon: -121.2908 };

export function ControlRoom({ go }: { go: (screen: "driver") => void }) {
  const sim = useSim();
  const [creating, setCreating] = useState(false);

  return (
    <>
      <ScreenHead
        title="Control room"
        lede="Create a load, send delay-risk pings and move the clock. Play the driver, dispatcher and broker from the other screens."
        actions={
          <button type="button" className="btn btn--primary" onClick={() => setCreating(true)}>
            New load
          </button>
        }
      />
      <div className="grid grid--control">
        <div className="col-main">
          <LoadCard onCreate={() => setCreating(true)} />
          <TmsNotes />
        </div>
        <div className="col-side">
          <PingInjector />
          <SimulationSettings />
        </div>
        <LoadsTable onOpen={(id) => { sim.selectLoad(id); go("driver"); }} />
      </div>
      <NewLoadDialog open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

function LoadCard({ onCreate }: { onCreate: () => void }) {
  const { snapshot, load, selectedLoadId, state } = useSim();
  const summary = state?.loads.find((l) => l.load_id === selectedLoadId);

  if (!selectedLoadId) {
    return (
      <Panel title="Load">
        <Empty
          title="Create a load from a fixture to start"
          action={
            <button type="button" className="btn btn--primary" onClick={onCreate}>
              New load
            </button>
          }
        >
          The assignment's load 481207 (Northline) and the Keystone and reefer variants are
          ready as fixtures.
        </Empty>
      </Panel>
    );
  }
  if (snapshot.error && !load) {
    return (
      <Panel title={`Load ${selectedLoadId}`}>
        <InlineError message={snapshot.error} onRetry={() => void snapshot.reload()} />
      </Panel>
    );
  }
  if (!load) {
    return (
      <Panel title={`Load ${selectedLoadId}`}>
        <p className="muted">Loading the load…</p>
      </Panel>
    );
  }
  return (
    <Panel
      title={
        <>
          Load {load.load_id}
          <span className="panel-title-sub">{load.broker_name}</span>
        </>
      }
      actions={
        <>
          <span className="chip chip--stage">{stageLabel(load.stage)}</span>
          <RiskChip risk={load.delay_risk} />
        </>
      }
    >
      <LoadFacts load={load} openObligations={summary?.open_obligations} pendingTimers={summary?.pending_timers} cost={summary?.cost_usd} />
      <h3 className="subhead">Stops</h3>
      <Stops load={load} />
      <h3 className="subhead">Instructions</h3>
      <p className="instructions">{load.instructions || <span className="muted">No instructions on this load.</span>}</p>
      <h3 className="subhead">Contacts</h3>
      <table className="table table--compact">
        <thead>
          <tr>
            <th scope="col">Role</th>
            <th scope="col">Contact id</th>
            <th scope="col">Channels</th>
          </tr>
        </thead>
        <tbody>
          {load.contacts.map((c) => (
            <tr key={c.id}>
              <td>{roleLabel(c.role)}</td>
              <td className="mono">{c.id}</td>
              <td>{c.channels.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

function LoadFacts(props: { load: Load; openObligations?: number; pendingTimers?: number; cost?: number }) {
  const { load } = props;
  return (
    <dl className="facts">
      <div>
        <dt>Shipper</dt>
        <dd>{load.shipper}</dd>
      </div>
      <div>
        <dt>Carrier</dt>
        <dd>
          {load.carrier_name} <span className="muted">MC {load.mc_number}</span>
        </dd>
      </div>
      <div className="facts-wide">
        <dt>Commodity</dt>
        <dd>{load.commodity}</dd>
      </div>
      <div>
        <dt>Client and load types</dt>
        <dd>
          <span className="mono">{load.broker_id}</span>
          {load.load_types.length ? `, ${load.load_types.join(", ")}` : <span className="muted">, no load type</span>}
        </dd>
      </div>
      <div>
        <dt>Agent memory</dt>
        <dd>
          {props.openObligations ?? 0} open request{props.openObligations === 1 ? "" : "s"}, {props.pendingTimers ?? 0} timer
          {props.pendingTimers === 1 ? "" : "s"} pending
        </dd>
      </div>
      <div>
        <dt>Model spend</dt>
        <dd>{usd(props.cost ?? 0)} of $1.00</dd>
      </div>
    </dl>
  );
}

function Stops({ load }: { load: Load }) {
  const current = currentStop(load);
  return (
    <ol className="stops">
      {[...load.stops]
        .sort((a, b) => a.sequence - b.sequence)
        .map((stop) => {
          const appt = stop.appointment;
          const when =
            appt.kind === "fixed"
              ? `${formatDateTime(appt.at, stop.timezone)}, fixed`
              : `${formatWindow(appt.start, appt.end, stop.timezone)}, first come first served`;
          const isCurrent = current?.sequence === stop.sequence;
          return (
            <li key={stop.sequence} className={clsx("stop", isCurrent && "stop--current")}>
              <span className="stop-seq" aria-hidden="true">
                {stop.sequence}
              </span>
              <div className="stop-body">
                <p className="stop-name">
                  {stop.type === "pickup" ? "Pickup" : "Delivery"}: {stop.name}
                  {isCurrent ? <span className="tag tag--sop">Next stop</span> : null}
                </p>
                <p className="stop-meta">{stop.address}</p>
                <p className="stop-appt">{when}</p>
              </div>
            </li>
          );
        })}
    </ol>
  );
}

function TmsNotes() {
  const { selectedLoadId, timezone, state } = useSim();
  const notes = useResource(selectedLoadId ? `tms:${selectedLoadId}` : null, () => api.tmsNotes(selectedLoadId as string), ["tms"]);
  if (!selectedLoadId) return null;
  return (
    <Panel title="TMS notes" flush>
      {notes.error ? <InlineError message={notes.error} onRetry={() => void notes.reload()} /> : null}
      {notes.data && notes.data.length === 0 ? (
        <Empty title="No TMS note yet">
          The agent writes here when an ETA arrives or the driver reports a status that matters.
        </Empty>
      ) : null}
      {notes.data?.length ? (
        <ul className="note-list">
          {notes.data.map((note, i) => (
            <li key={`${note.at}-${i}`}>
              <time className="note-time" dateTime={note.at}>
                {formatStamp(note.at, timezone, state?.now)}
              </time>
              <span>{note.text}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Panel>
  );
}

function PingInjector() {
  const sim = useSim();
  const toasts = useToasts();
  const [risk, setRisk] = useState<DelayRisk>("high");
  const [lat, setLat] = useState(String(DEFAULT_POSITION.lat));
  const [lon, setLon] = useState(String(DEFAULT_POSITION.lon));
  const disabled = !sim.selectedLoadId || !sim.state || sim.busy;

  async function send() {
    if (!sim.selectedLoadId || !sim.state) return;
    const result = await sim.perform(() =>
      api.sendPing(
        {
          load_id: sim.selectedLoadId as string,
          lat: Number(lat),
          lon: Number(lon),
          occurred_at: sim.state!.now,
          delay_risk: risk,
        },
        sim.latencySeconds,
      ),
    );
    if (result) {
      if (result.duplicate) toasts.info("That ping was already received; nothing changed.");
      else if (!result.accepted) toasts.error(result.reason ?? "The ping was not accepted.");
      else toasts.info(`Ping sent: ${risk} risk.`);
    }
  }

  return (
    <Panel title="Delay-risk ping">
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <fieldset className="segmented" disabled={disabled}>
          <legend className="field-label">Delay risk the ping reports</legend>
          {RISKS.map((r) => (
            <label key={r} className={clsx("segment", `segment--${r}`, risk === r && "segment--on")}>
              <input type="radio" name="risk" value={r} checked={risk === r} onChange={() => setRisk(r)} />
              {r.charAt(0).toUpperCase() + r.slice(1)}
            </label>
          ))}
        </fieldset>
        <div className="row">
          <label className="field">
            <span className="field-label">Latitude</span>
            <input className="input" inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Longitude</span>
            <input className="input" inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} />
          </label>
        </div>
        <button type="submit" className="btn btn--primary" disabled={disabled || !Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))}>
          Send ping
        </button>
        <p className="hint">
          Only a change into or out of high wakes the agent. Medium is recorded but starts no run.
          The ping is stamped with the sim time; its run follows after the inbound latency.
        </p>
      </form>
    </Panel>
  );
}

function SimulationSettings() {
  const sim = useSim();
  return (
    <Panel title="Simulation">
      <dl className="facts facts--single">
        <div>
          <dt>Model</dt>
          <dd>{sim.state?.model_ready === false ? "no API key" : "live"}</dd>
        </div>
        <div>
          <dt>Events queued</dt>
          <dd>{sim.state?.queue_size ?? 0}</dd>
        </div>
      </dl>
      <label className="field">
        <span className="field-label">Inbound latency, seconds of sim time</span>
        <input
          className="input"
          type="number"
          min={0}
          max={3600}
          value={sim.latencySeconds}
          onChange={(e) => sim.setLatencySeconds(Math.max(0, Math.min(3600, Number(e.target.value) || 0)))}
        />
      </label>
      <p className="hint">How long after a message or ping the agent's run happens. The evals use 60.</p>
    </Panel>
  );
}

function LoadsTable({ onOpen }: { onOpen: (loadId: string) => void }) {
  const { loads, selectedLoadId, selectLoad } = useSim();
  return (
    <Panel title="All loads" className="span-all" flush>
      {loads.length === 0 ? (
        <Empty title="No loads in this simulation">Use New load to create one from a fixture.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Load</th>
                <th scope="col">Client</th>
                <th scope="col">Stage</th>
                <th scope="col">Risk</th>
                <th scope="col" className="num">Open requests</th>
                <th scope="col" className="num">Timers</th>
                <th scope="col" className="num">Model spend</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {loads.map((l) => (
                <tr key={l.load_id} className={clsx(l.load_id === selectedLoadId && "row--selected")}>
                  <td className="mono">{l.load_id}</td>
                  <td>{l.broker_name}</td>
                  <td>{stageLabel(l.stage)}</td>
                  <td>
                    <RiskChip risk={l.delay_risk} />
                  </td>
                  <td className="num">{l.open_obligations}</td>
                  <td className="num">{l.pending_timers}</td>
                  <td className="num">{usd(l.cost_usd)}</td>
                  <td className="actions-cell">
                    {l.load_id === selectedLoadId ? (
                      <span className="muted">Selected</span>
                    ) : (
                      <button type="button" className="btn btn--small" onClick={() => selectLoad(l.load_id)}>
                        Select
                      </button>
                    )}
                    <button type="button" className="btn btn--small btn--quiet" onClick={() => onOpen(l.load_id)}>
                      Open driver phone
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function NewLoadDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const sim = useSim();
  const toasts = useToasts();
  const fixtures = useResource(open ? "fixtures" : null, () => api.fixtures(), []);
  const [chosen, setChosen] = useState<string>("");
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);

  const list: Fixture[] = useMemo(() => fixtures.data ?? [], [fixtures.data]);
  const pick = useCallback(
    (name: string) => {
      setChosen(name);
      const fixture = list.find((f) => f.name === name);
      setText(fixture ? JSON.stringify(fixture.metadata, null, 2) : "");
      setParseError(null);
    },
    [list],
  );
  useEffect(() => {
    if (open && !chosen && list[0]) pick(list[0].name);
  }, [open, chosen, list, pick]);

  async function create() {
    let metadata: LoadMetadata;
    try {
      const parsed: unknown = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("The metadata must be a JSON object.");
      metadata = parsed as LoadMetadata;
    } catch (error) {
      setParseError(error instanceof Error ? error.message : String(error));
      return;
    }
    const summary = await sim.perform(() => api.createLoad(metadata));
    if (summary) {
      sim.addLoad(summary);
      toasts.info(`Load ${summary.load_id} created for ${summary.broker_name}.`);
      setChosen("");
      onClose();
    }
  }

  return (
    <Modal
      open={open}
      wide
      title="New load"
      submitLabel="Create load"
      submitDisabled={!text.trim() || sim.busy}
      onClose={() => {
        setChosen("");
        onClose();
      }}
      onSubmit={() => void create()}
    >
      {fixtures.error ? <InlineError message={fixtures.error} onRetry={() => void fixtures.reload()} /> : null}
      <label className="field">
        <span className="field-label">Fixture</span>
        <select className="input" value={chosen} onChange={(e) => pick(e.target.value)} disabled={!list.length}>
          {!list.length ? <option value="">{fixtures.loading ? "Loading fixtures…" : "No fixture found"}</option> : null}
          {list.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="field-label">Load metadata (assignment JSON, editable; change load_id to add a second copy)</span>
        <textarea
          className="input mono json-editor"
          value={text}
          rows={16}
          spellCheck={false}
          onChange={(e) => {
            setText(e.target.value);
            setParseError(null);
          }}
          aria-invalid={parseError ? true : undefined}
          aria-describedby={parseError ? "json-error" : undefined}
        />
      </label>
      {parseError ? (
        <p className="field-error" id="json-error" role="alert">
          {parseError}
        </p>
      ) : null}
    </Modal>
  );
}
