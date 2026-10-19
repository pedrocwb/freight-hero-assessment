import clsx from "clsx";

import type { Health, RunStatus } from "../api";
import { Empty, InlineError, LevelChip, Meter, Panel, ScreenHead, percent, usd } from "../components/ui";
import type { Resource } from "../hooks/useResource";
import { humanize } from "../lib/load";
import { formatDuration } from "../lib/time";

const STATUSES: RunStatus[] = ["acted", "noop", "skipped", "escalated", "failed"];
const SECRET = /(KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL)/i;
const RUN_P95_WARN_S = 60;
const AGENT_RUN_LIMIT_S = 300;

/** A secret is only ever shown as set or unset, even if the API sent more. */
export function displaySetting(name: string, value: string): string {
  if (!SECRET.test(name)) return value;
  return value && value !== "unset" ? "set" : "unset";
}

export function HealthScreen({ resource }: { resource: Resource<Health> }) {
  const health = resource.data;
  return (
    <>
      <ScreenHead
        title="Health"
        lede="Computed from the agent's own records (runs, events, ledger, outbox), so it keeps working when tracing is off."
        actions={
          <button type="button" className="btn" onClick={() => void resource.reload()} disabled={resource.loading}>
            {resource.loading ? "Refreshing…" : "Refresh"}
          </button>
        }
      />
      {resource.error ? <InlineError message={resource.error} onRetry={() => void resource.reload()} /> : null}
      {!health && !resource.error ? <p className="muted">Loading health…</p> : null}
      {health ? (
        <>
          <section className={clsx("status-banner", `status-banner--${health.status}`)} aria-labelledby="health-status">
            <h2 className="status-title" id="health-status">
              <LevelChip level={health.status} />
              {health.status === "ok"
                ? "All checks pass"
                : health.reasons.length === 1
                  ? "1 check needs attention"
                  : `${health.reasons.length} checks need attention`}
            </h2>
            {health.reasons.length ? (
              <ul className="reasons">
                {health.reasons.map((r, i) => (
                  <li key={`${r.check}-${i}`} className="reason">
                    <LevelChip level={r.level} />
                    <span className="mono reason-check">{r.check}</span>
                    <span>{r.message}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
          <div className="grid grid--health">
            <QueuePanel health={health} />
            <RunsPanel health={health} />
            <CostPanel health={health} />
            <OutboxPanel health={health} />
            <SettingsPanel health={health} />
          </div>
        </>
      ) : null}
    </>
  );
}

function QueuePanel({ health }: { health: Health }) {
  const q = health.queue;
  if (!q) {
    return (
      <Panel title="Queue">
        <Empty title="No queue data" />
      </Panel>
    );
  }
  return (
    <Panel title="Queue">
      <dl className="stats">
        <div>
          <dt>Queued</dt>
          <dd>{q.queued}</dd>
        </div>
        <div>
          <dt>Dead letters</dt>
          <dd className={clsx(q.dead_letters > 0 && "text-warn")}>{q.dead_letters}</dd>
        </div>
      </dl>
      <Meter
        label="Oldest queued event against the SLA"
        value={q.oldest_age_s ?? 0}
        max={q.sla_s || AGENT_RUN_LIMIT_S}
        warnAt={0.6}
        valueText={
          q.oldest_age_s === null ? "queue empty" : `${formatDuration(q.oldest_age_s)} of ${formatDuration(q.sla_s)}`
        }
      />
      {q.oldest_event_id ? (
        <p className="hint">
          Oldest: <span className="mono">{q.oldest_event_id}</span>
        </p>
      ) : null}
    </Panel>
  );
}

function RunsPanel({ health }: { health: Health }) {
  const r = health.runs;
  if (!r) {
    return (
      <Panel title="Runs">
        <Empty title="No run data" />
      </Panel>
    );
  }
  return (
    <Panel title="Runs">
      <dl className="stats">
        <div>
          <dt>Runs</dt>
          <dd>{r.total}</dd>
        </div>
        <div>
          <dt>Duration p50</dt>
          <dd>{formatDuration(r.duration_p50_s)}</dd>
        </div>
        <div>
          <dt>Duration p95</dt>
          <dd className={clsx((r.duration_p95_s ?? 0) > RUN_P95_WARN_S && "text-warn")}>
            {formatDuration(r.duration_p95_s)}
          </dd>
        </div>
        <div>
          <dt>Validator blocks</dt>
          <dd>
            {r.blocked_runs} <span className="muted">of {r.planned} plans</span>
          </dd>
        </div>
      </dl>
      <Meter
        label="Run p95 against the 5-minute limit"
        value={r.duration_p95_s ?? 0}
        max={AGENT_RUN_LIMIT_S}
        warnAt={RUN_P95_WARN_S / AGENT_RUN_LIMIT_S}
        valueText={`${formatDuration(r.duration_p95_s)} of 5 min`}
      />
      <table className="table table--compact">
        <caption className="visually-hidden">Runs by status</caption>
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col" className="num">
              Runs
            </th>
            <th scope="col" className="num">
              Share
            </th>
          </tr>
        </thead>
        <tbody>
          {STATUSES.map((s) => (
            <tr key={s}>
              <td>{humanize(s)}</td>
              <td className="num">{r.status_counts[s] ?? 0}</td>
              <td className="num">{percent(r.status_rates[s] ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.models.length ? (
        <table className="table table--compact">
          <caption className="visually-hidden">Model errors</caption>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Calls
              </th>
              <th scope="col" className="num">
                Errors
              </th>
            </tr>
          </thead>
          <tbody>
            {r.models.map((m) => (
              <tr key={m.model}>
                <td className="mono">{m.model}</td>
                <td className="num">{m.calls}</td>
                <td className={clsx("num", m.errors > 0 && "text-warn")}>
                  {m.errors} <span className="muted">({percent(m.error_rate)})</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Panel>
  );
}

function CostPanel({ health }: { health: Health }) {
  const c = health.cost;
  if (!c) {
    return (
      <Panel title="Cost per load">
        <Empty title="No cost data" />
      </Panel>
    );
  }
  return (
    <Panel title="Cost per load">
      <dl className="stats">
        <div>
          <dt>Total</dt>
          <dd>{usd(c.total_cost_usd)}</dd>
        </div>
        <div>
          <dt>Per run, average</dt>
          <dd>{usd(c.avg_cost_per_run_usd, 5)}</dd>
        </div>
        <div>
          <dt>Budget per load</dt>
          <dd>{usd(c.budget_usd, 2)}</dd>
        </div>
      </dl>
      {c.loads.length === 0 ? (
        <p className="muted">No run has cost anything yet.</p>
      ) : (
        <div className="stack">
          {c.loads.map((l) => (
            <Meter
              key={l.load_id}
              label={`${l.load_id}, ${l.runs} run${l.runs === 1 ? "" : "s"}`}
              value={l.cost_usd}
              max={c.budget_usd}
              warnAt={0.8}
              valueText={`${usd(l.cost_usd)} (${percent(l.budget_used)})`}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

function OutboxPanel({ health }: { health: Health }) {
  const o = health.outbox;
  if (!o) {
    return (
      <Panel title="Outbox delivery">
        <Empty title="No outbox data" />
      </Panel>
    );
  }
  return (
    <Panel title="Outbox delivery">
      <dl className="stats">
        <div>
          <dt>Pending</dt>
          <dd>{o.pending}</dd>
        </div>
        <div>
          <dt>Failed</dt>
          <dd className={clsx(o.failed > 0 && "text-warn")}>{o.failed}</dd>
        </div>
        <div>
          <dt>Stuck</dt>
          <dd className={clsx(o.stuck > 0 && "text-critical")}>{o.stuck}</dd>
        </div>
        <div>
          <dt>Oldest pending</dt>
          <dd>{o.oldest_pending_age_s === null ? "none" : formatDuration(o.oldest_pending_age_s)}</dd>
        </div>
      </dl>
      {o.failed_ids.length ? (
        <p className="hint">
          Failed: <span className="mono">{o.failed_ids.join(", ")}</span>
        </p>
      ) : null}
    </Panel>
  );
}

function SettingsPanel({ health }: { health: Health }) {
  const settings = Object.entries(health.settings ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Panel title="Settings" flush className="span-2">
      {settings.length === 0 ? (
        <Empty title="No settings reported" />
      ) : (
        <table className="table table--compact">
          <caption className="visually-hidden">Settings; secrets show only as set or unset</caption>
          <thead>
            <tr>
              <th scope="col">Variable</th>
              <th scope="col">Value</th>
            </tr>
          </thead>
          <tbody>
            {settings.map(([name, value]) => {
              const shown = displaySetting(name, String(value));
              const secret = SECRET.test(name);
              return (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td>
                    {secret ? (
                      <span className={clsx("chip", shown === "set" ? "chip--obl-satisfied" : "chip--neutral")}>
                        {shown}
                      </span>
                    ) : (
                      <span className="mono">{shown}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
