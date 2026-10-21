import { useEffect, useRef } from "react";

import { api, type LoadSnapshot } from "../api";
import { useResource } from "../hooks/useResource";
import { humanize, phrase, roleLabel, stageLabel } from "../lib/load";
import { formatStamp, formatWindow } from "../lib/time";

/**
 * What the agent keeps about the load, out of the way of the conversation: its open
 * requests and timers, the documents it read, the facility notes, and the load itself.
 */
export function LoadFacts(props: {
  open: boolean;
  onClose: () => void;
  snapshot: LoadSnapshot;
  caps: Readonly<Record<string, number | null>>;
  timezone: string;
  now: string | undefined;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const { snapshot, timezone, now } = props;
  const load = snapshot.load;
  const docs = useResource(props.open ? `documents:${load.load_id}` : null, () => api.loadDocuments(load.load_id), ["snapshot"]);
  const notes = useResource(props.open ? `facility:${load.load_id}` : null, () => api.facilityMemory(load.load_id), ["snapshot"]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (props.open && !dialog.open) dialog.showModal();
    if (!props.open && dialog.open) dialog.close();
  }, [props.open]);

  const timers = new Map(snapshot.timers.map((t) => [t.timer_id, t]));
  const obligations = [...snapshot.obligations].sort(
    (a, b) => Number(b.status === "open") - Number(a.status === "open") || Date.parse(b.opened_at) - Date.parse(a.opened_at),
  );
  const pending = snapshot.timers.filter((t) => t.status === "pending").sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  const done = snapshot.timers.length - pending.length;
  const facts = (notes.data ?? []).flatMap((s) => s.facts.map((f) => ({ stop: s.name, fact: f })));

  return (
    <dialog ref={ref} className="sheet" aria-labelledby="facts-title" onClose={props.onClose} onCancel={props.onClose}>
      <header className="sheet-head">
        <h2 id="facts-title">Load facts</h2>
        <button type="button" className="icon-button" onClick={props.onClose} aria-label="Close the load facts">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="m5 5 10 10M15 5 5 15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <section className="sheet-section">
        <h3 className="why-label">Requests</h3>
        {obligations.length ? (
          <ul className="rows">
            {obligations.map((o) => {
              const timer = o.active_timer_id ? timers.get(o.active_timer_id) : undefined;
              const cap = props.caps[o.kind];
              return (
                <li key={o.obligation_id}>
                  <span className="row-main">
                    {humanize(o.kind)}, {o.status}
                  </span>
                  <span className="row-sub">
                    {o.attempts}
                    {cap ? ` of ${cap}` : ""} attempt{(cap ?? o.attempts) === 1 ? "" : "s"}
                    {o.deferred_until ? `, waiting until ${formatStamp(o.deferred_until, timezone, now)}` : ""}
                    {timer && timer.status === "pending" ? `, follow-up ${formatStamp(timer.due_at, timezone, now)}` : ""}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="why-sub">Nothing asked yet. A request opens when the agent asks the driver for an ETA or a document.</p>
        )}
      </section>

      <section className="sheet-section">
        <h3 className="why-label">Timers</h3>
        {pending.length ? (
          <ul className="rows">
            {pending.map((t) => {
              const kind = snapshot.obligations.find((o) => o.obligation_id === t.obligation_id)?.kind;
              return (
                <li key={t.timer_id}>
                  <span className="row-main">{formatStamp(t.due_at, timezone, now)}</span>
                  <span className="row-sub">follow up on the {kind ? phrase(kind) : "request"}</span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="why-sub">None pending.</p>
        )}
        {done ? <p className="why-sub">{done} earlier timer{done === 1 ? "" : "s"} fired or replaced.</p> : null}
      </section>

      <section className="sheet-section">
        <h3 className="why-label">Documents</h3>
        {docs.data?.length ? (
          <ul className="rows">
            {docs.data.map((d) => {
              const failed = d.checks.filter((c) => !c.ok);
              return (
                <li key={d.document_id}>
                  <span className="row-main">
                    {d.type_id ? humanize(d.type_id) : "Unclassified"}, {humanize(d.status).toLowerCase()}
                  </span>
                  <span className="row-sub">
                    {d.filename}
                    {failed.length ? `; ${failed.length} check${failed.length === 1 ? "" : "s"} failed: ${failed.map((c) => humanize(c.field).toLowerCase()).join(", ")}` : d.checks.length ? "; every check matched the load" : ""}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="why-sub">{docs.loading ? "Loading…" : "None. Attach a file to a message to send one."}</p>
        )}
      </section>

      <section className="sheet-section">
        <h3 className="why-label">Facility notes</h3>
        {facts.length ? (
          <ul className="rows">
            {facts.map(({ stop, fact }) => (
              <li key={fact.fact_id}>
                <span className="row-main">{fact.text}</span>
                <span className="row-sub">
                  {stop}, told on load {fact.source_load_id}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="why-sub">{notes.loading ? "Loading…" : "None yet. A delivered load of this client leaves what its driver said about each stop."}</p>
        )}
      </section>

      <section className="sheet-section">
        <h3 className="why-label">The load</h3>
        <dl className="facts">
          <div>
            <dt>Client</dt>
            <dd>{load.broker_name}</dd>
          </div>
          <div>
            <dt>Carrier</dt>
            <dd>
              {load.carrier_name}, MC {load.mc_number}
            </dd>
          </div>
          <div>
            <dt>Freight</dt>
            <dd>{load.commodity}</dd>
          </div>
          <div>
            <dt>Stage</dt>
            <dd>{stageLabel(load.stage)}</dd>
          </div>
        </dl>
        <ul className="rows">
          {load.stops.map((s) => (
            <li key={s.sequence}>
              <span className="row-main">
                {s.type === "pickup" ? "Pickup" : "Delivery"}: {s.name}, {s.address}
              </span>
              <span className="row-sub">
                {s.appointment.kind === "window"
                  ? formatWindow(s.appointment.start, s.appointment.end, s.timezone)
                  : formatStamp(s.appointment.at, s.timezone, null)}
              </span>
            </li>
          ))}
        </ul>
        <ul className="rows">
          {load.contacts.map((c) => (
            <li key={c.id}>
              <span className="row-main">{roleLabel(c.role)}</span>
              <span className="row-sub">
                {c.id}, {c.channels.map((ch) => (ch === "sms" ? "SMS" : humanize(ch).toLowerCase())).join(", ")}
              </span>
            </li>
          ))}
        </ul>
        {load.instructions ? <p className="why-sub sheet-instructions">{load.instructions}</p> : null}
      </section>
    </dialog>
  );
}
