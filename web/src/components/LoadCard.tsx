import clsx from "clsx";

import type { Load } from "../api";
import { currentStop, roleLabel } from "../lib/load";
import { formatDateTime, formatWindow } from "../lib/time";
import { Panel } from "./ui";

/** The load's facts the agent reasons with: stops, instructions and contacts. */
export function LoadCard({ load }: { load: Load }) {
  return (
    <Panel title="Load" flush>
      <div className="panel-pad stack">
        <dl className="facts facts--single">
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
          <div>
            <dt>Client</dt>
            <dd>
              <span className="mono">{load.broker_id}</span>
              {load.load_types.length ? `, ${load.load_types.join(", ")}` : null}
            </dd>
          </div>
          <div>
            <dt>Commodity</dt>
            <dd>{load.commodity}</dd>
          </div>
        </dl>
        <Stops load={load} />
        {load.instructions ? <p className="instructions">{load.instructions}</p> : null}
      </div>
      <table className="table table--compact">
        <caption className="visually-hidden">Contacts</caption>
        <thead>
          <tr>
            <th scope="col">Contact</th>
            <th scope="col">Role</th>
            <th scope="col">Channels</th>
          </tr>
        </thead>
        <tbody>
          {load.contacts.map((c) => (
            <tr key={c.id}>
              <td className="mono">{c.id}</td>
              <td>{roleLabel(c.role)}</td>
              <td>{c.channels.join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
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
