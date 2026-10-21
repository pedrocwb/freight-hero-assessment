import { api, type FacilityFact } from "../api";
import { useResource } from "../hooks/useResource";
import { humanize } from "../lib/load";
import { formatStamp } from "../lib/time";
import { useSim } from "../state/sim";
import { Empty, InlineError, Panel } from "./ui";

/**
 * The selected load's facility memory, read-only: for each stop, the notes earlier loads
 * of the same client left about the place (where to check in, the hours, what the gate
 * asks for). A delivered load adds its own once the extraction has read it.
 */
export function FacilityMemoryPanel() {
  const { selectedLoadId, timezone, state } = useSim();
  const stops = useResource(
    selectedLoadId ? `facility-memory:${selectedLoadId}` : null,
    () => api.facilityMemory(selectedLoadId as string),
    ["snapshot"],
  );
  const total = (stops.data ?? []).reduce((n, s) => n + s.facts.length, 0);

  return (
    <Panel title={`Facility notes${stops.data ? `, ${total}` : ""}`} flush>
      {stops.error ? <InlineError message={stops.error} onRetry={() => void stops.reload()} /> : null}
      {stops.data && total === 0 ? (
        <Empty title="No note yet">
          When a load of this client is delivered, what its driver said about a stop (where to
          check in, the hours, what the gate asks for) is kept here for the next load.
        </Empty>
      ) : null}
      <ul className="doc-list">
        {(stops.data ?? [])
          .filter((s) => s.facts.length > 0)
          .map((stop) => (
            <li key={stop.sequence} className="doc">
              <div className="ledger-head">
                <span className="ledger-kind">
                  Stop {stop.sequence}, {stop.name}
                </span>
                <span className="chip chip--neutral">{humanize(stop.type)}</span>
              </div>
              <ul className="note-list">
                {stop.facts.map((fact) => (
                  <Note key={fact.fact_id} fact={fact} timezone={timezone} now={state?.now} />
                ))}
              </ul>
            </li>
          ))}
      </ul>
    </Panel>
  );
}

function Note({ fact, timezone, now }: { fact: FacilityFact; timezone: string; now: string | undefined }) {
  return (
    <li>
      <p>{fact.text}</p>
      <p className="cell-sub">
        {humanize(fact.kind)}, told on load <span className="mono">{fact.source_load_id}</span>{" "}
        {formatStamp(fact.source_at, timezone, now)}
      </p>
    </li>
  );
}
