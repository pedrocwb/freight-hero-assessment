import clsx from "clsx";

import type { LoadSnapshot, Obligation, Timer } from "../api";
import { humanize } from "../lib/load";
import { formatRelative, formatStamp, minutesBetween } from "../lib/time";
import { useSim } from "../state/sim";
import { Empty, Panel } from "./ui";

const OBLIGATION_LABEL: Record<Obligation["status"], string> = {
  open: "Open",
  satisfied: "Satisfied",
  exhausted: "Exhausted",
  cancelled: "Cancelled",
};

/** What the agent still owes or is owed: the ledger, facts only. */
export function LedgerPanel({ snapshot, compact }: { snapshot: LoadSnapshot | undefined; compact?: boolean }) {
  const { timezone, state } = useSim();
  const now = state?.now;
  const obligations = [...(snapshot?.obligations ?? [])].sort(
    (a, b) => Number(b.status === "open") - Number(a.status === "open") || Date.parse(b.opened_at) - Date.parse(a.opened_at),
  );
  const timers = new Map((snapshot?.timers ?? []).map((t) => [t.timer_id, t]));

  return (
    <Panel title="Ledger" flush>
      {obligations.length === 0 ? (
        <Empty title="Nothing open">
          The agent opens an ETA request when it texts the driver for an ETA. It stays here,
          with its attempts and follow-up timer, until it is satisfied, exhausted or cancelled.
        </Empty>
      ) : (
        <ul className="ledger">
          {obligations.map((o) => {
            const active = o.active_timer_id ? timers.get(o.active_timer_id) : undefined;
            return (
              <li key={o.obligation_id} className={clsx("ledger-item", o.status === "open" && "ledger-item--open")}>
                <div className="ledger-head">
                  <span className="ledger-kind">{humanize(o.kind)}</span>
                  <span className={clsx("chip", `chip--obl-${o.status}`)}>{OBLIGATION_LABEL[o.status]}</span>
                </div>
                <dl className="ledger-facts">
                  <div>
                    <dt>Attempts</dt>
                    <dd className="num-strong">{o.attempts}</dd>
                  </div>
                  <div>
                    <dt>Deferred until</dt>
                    <dd>
                      {o.deferred_until ? formatStamp(o.deferred_until, timezone, now) : <span className="muted">not deferred</span>}
                      {o.deferrals_used ? <span className="muted"> ({o.deferrals_used} used)</span> : null}
                    </dd>
                  </div>
                  <div>
                    <dt>Active timer</dt>
                    <dd>
                      {active ? (
                        <>
                          {formatStamp(active.due_at, timezone, now)}
                          {now ? <span className="muted">, {formatRelative(minutesBetween(now, active.due_at))}</span> : null}
                        </>
                      ) : o.active_timer_id ? (
                        <span className="mono">{o.active_timer_id}</span>
                      ) : (
                        <span className="muted">none</span>
                      )}
                    </dd>
                  </div>
                  {!compact ? (
                    <>
                      <div>
                        <dt>Opened</dt>
                        <dd>{formatStamp(o.opened_at, timezone, now)}</dd>
                      </div>
                      <div>
                        <dt>Last attempt</dt>
                        <dd>{o.last_action_at ? formatStamp(o.last_action_at, timezone, now) : <span className="muted">none</span>}</dd>
                      </div>
                      <div>
                        <dt>Id</dt>
                        <dd className="mono">{o.obligation_id}</dd>
                      </div>
                    </>
                  ) : null}
                </dl>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

const TIMER_ORDER: Record<Timer["status"], number> = { pending: 0, fired: 1, superseded: 2, cancelled: 3 };

export function TimersPanel({ snapshot }: { snapshot: LoadSnapshot | undefined }) {
  const { timezone, state } = useSim();
  const now = state?.now;
  const timers = [...(snapshot?.timers ?? [])].sort(
    (a, b) => TIMER_ORDER[a.status] - TIMER_ORDER[b.status] || Date.parse(a.due_at) - Date.parse(b.due_at),
  );
  const active = new Set((snapshot?.obligations ?? []).map((o) => o.active_timer_id).filter(Boolean));
  const pending = timers.filter((t) => t.status === "pending").length;

  return (
    <Panel title={`Timers, ${pending} pending`} flush>
      {timers.length === 0 ? (
        <Empty title="No timer set">The agent sets a follow-up timer with each ETA request.</Empty>
      ) : (
        <table className="table table--compact">
          <thead>
            <tr>
              <th scope="col">Due</th>
              <th scope="col">Status</th>
              <th scope="col">Timer</th>
            </tr>
          </thead>
          <tbody>
            {timers.map((t) => (
              <tr key={t.timer_id} className={clsx(t.status !== "pending" && "row--muted")}>
                <td>
                  {formatStamp(t.due_at, timezone, now)}
                  {t.status === "pending" && now ? (
                    <span className="cell-sub">{formatRelative(minutesBetween(now, t.due_at))}</span>
                  ) : null}
                </td>
                <td>
                  <span className={clsx("chip", `chip--timer-${t.status}`)}>{humanize(t.status)}</span>
                  {active.has(t.timer_id) ? <span className="cell-sub">active</span> : null}
                </td>
                <td className="mono">{t.timer_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
