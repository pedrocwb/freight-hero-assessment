import clsx from "clsx";
import { useState } from "react";

import { runCost, runLatencyMs, runTokens, type Action, type Load, type LoadEvent, type RunRecord, type TrajectoryStep } from "../api";
import { DocumentsPanel } from "../components/Documents";
import { LedgerPanel, TimersPanel } from "../components/Ledger";
import { Empty, InlineError, Meter, NoLoad, Panel, ScreenHead, StatusChip, Tag, usd } from "../components/ui";
import { channelLabel, humanize, roleLabel, stageLabel } from "../lib/load";
import { formatDuration, formatTimeSeconds } from "../lib/time";
import { clip, compactArgs, rejectionRule, stepVerdict, tallyTrajectory } from "../lib/trajectory";
import { useSim } from "../state/sim";

const BUDGET_USD = 1;

export function Inspector() {
  const sim = useSim();
  const { snapshot } = sim;
  const runs = [...(snapshot.data?.runs ?? [])].sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const latest = runs[runs.length - 1]?.run_id;
  const isOpen = (id: string) => open[id] ?? id === latest;
  const allOpen = runs.length > 0 && runs.every((r) => isOpen(r.run_id));

  if (!sim.selectedLoadId) {
    return (
      <>
        <ScreenHead title="Agent inspector" />
        <NoLoad what="every agent run" />
      </>
    );
  }

  const total = runs.reduce((sum, r) => sum + runCost(r), 0);

  return (
    <>
      <ScreenHead
        title="Agent inspector"
        lede="Each run of the agent on this load: what woke it, the intent, the broker config it ran on, every tool call it made and what the rules said, the plan, and what the validator dropped."
        actions={
          runs.length ? (
            <button
              type="button"
              className="btn"
              onClick={() => setOpen(Object.fromEntries(runs.map((r) => [r.run_id, !allOpen])))}
            >
              {allOpen ? "Collapse all" : "Expand all"}
            </button>
          ) : null
        }
      />
      <div className="grid grid--inspector">
        <section className="col-main" aria-labelledby="runs-title">
          <h2 className="visually-hidden" id="runs-title">
            Runs
          </h2>
          {snapshot.error ? <InlineError message={snapshot.error} onRetry={() => void snapshot.reload()} /> : null}
          {snapshot.data && runs.length === 0 ? (
            <Panel title="Runs">
              <Empty title="No run yet">
                A run starts for each ping that changes the risk band, each message from a contact and
                each timer that comes due. Send a high-risk ping from the Control room.
              </Empty>
            </Panel>
          ) : null}
          {runs.length ? (
            <p className="runs-summary">
              {runs.length} run{runs.length === 1 ? "" : "s"}, {usd(total)} in model calls
            </p>
          ) : null}
          <ol className="runs">
            {runs.map((run, index) => (
              <RunCard
                key={run.run_id}
                run={run}
                number={index + 1}
                event={snapshot.data?.events.find((e) => e.event_id === run.event_id)}
                load={sim.load}
                open={isOpen(run.run_id)}
                onToggle={() => setOpen((o) => ({ ...o, [run.run_id]: !isOpen(run.run_id) }))}
              />
            ))}
          </ol>
        </section>
        <div className="col-side">
          <LedgerPanel snapshot={snapshot.data} />
          <DocumentsPanel />
          <TimersPanel snapshot={snapshot.data} />
          <BudgetPanel />
        </div>
      </div>
    </>
  );
}

function describeEvent(event: LoadEvent | undefined, load: Load | undefined, eventId: string): string {
  if (!event) return `Event ${eventId}`;
  const data = event.data ?? {};
  switch (event.type) {
    case "risk_changed":
      return `Delay risk ${String(data.previous ?? "?")} to ${String(data.current ?? "?")}`;
    case "timer_due":
      return `Follow-up timer due (${humanize(String(data.kind ?? "timer"))})`;
    case "inbound_message": {
      const role = load?.contacts.find((c) => c.id === event.sender_contact_id)?.role;
      const from = event.sender_contact_id ? `${roleLabel(role)} ${event.sender_contact_id}` : "Unknown sender";
      const files = event.attachments.length ? `, with ${event.attachments.map((a) => a.filename).join(", ")}` : "";
      return `${from} by ${channelLabel(event.channel)}${files}`;
    }
    case "attachment_received":
      return `Attachment ${event.attachments.map((a) => a.filename).join(", ")}`;
    case "document_processed": {
      const type = typeof data.type_id === "string" && data.type_id ? humanize(data.type_id) : "Document";
      return `${type} read by the pipeline (${humanize(String(data.status ?? "processed")).toLowerCase()})`;
    }
    case "hero_update":
      return "Hero update";
    default:
      return humanize(event.type);
  }
}

function RunCard(props: {
  run: RunRecord;
  number: number;
  event: LoadEvent | undefined;
  load: Load | undefined;
  open: boolean;
  onToggle: () => void;
}) {
  const { run, event, load } = props;
  const { timezone } = useSim();
  const tokens = runTokens(run);
  const cost = runCost(run);
  const duration =
    run.finished_at !== null ? (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000 : null;
  const bodyId = `run-${run.run_id}`;
  const dropped = new Map<number, RunRecord["violations"]>();
  for (const v of run.violations) dropped.set(v.action_index, [...(dropped.get(v.action_index) ?? []), v]);
  const trajectory = run.trajectory ?? [];
  const tally = tallyTrajectory(trajectory);
  const searched = run.sections_searched ?? [];
  const intent = run.intent ?? run.gate?.intent ?? null;

  return (
    <li className={clsx("run", `run--${run.status}`)}>
      <h3 className="run-head">
        <button type="button" className="run-toggle" aria-expanded={props.open} aria-controls={bodyId} onClick={props.onToggle}>
          <span className="run-num">Run {props.number}</span>
          <time className="run-time" dateTime={run.started_at}>
            {formatTimeSeconds(run.started_at, timezone)}
          </time>
          <StatusChip status={run.status} />
          <span className="run-trigger">
            {describeEvent(event, load, run.event_id)}
            {intent ? <span className="run-intent mono">{intent}</span> : null}
          </span>
          <span className="run-cost">{usd(cost)}</span>
          <span className="run-caret" aria-hidden="true" />
        </button>
      </h3>
      {props.open ? (
        <div className="run-body" id={bodyId}>
          <dl className="run-grid">
            <div className="run-cell run-cell--wide">
              <dt>Trigger</dt>
              <dd>
                <span className="mono">{run.event_id}</span>
                {event?.body ? <blockquote className="quote">{event.body}</blockquote> : null}
                {event?.type === "timer_due" && typeof event.data.due_at === "string" ? (
                  <span className="cell-sub">due {formatTimeSeconds(event.data.due_at, timezone)}, fired {formatTimeSeconds(event.occurred_at, timezone)}</span>
                ) : null}
              </dd>
            </div>
            <div className="run-cell">
              <dt>Intent</dt>
              <dd>
                {intent ? (
                  <>
                    <span className="strong">{humanize(intent)}</span> <span className="mono muted">{intent}</span>
                    {run.gate && run.gate.intent !== intent ? (
                      <span className="cell-sub">the gate read {humanize(run.gate.intent).toLowerCase()}</span>
                    ) : null}
                  </>
                ) : (
                  <span className="muted">not labelled</span>
                )}
              </dd>
            </div>
            <div className="run-cell">
              <dt>Gate</dt>
              <dd>
                {run.gate ? (
                  <>
                    <span className="strong">{humanize(run.gate.intent)}</span>, {run.gate.act ? "acts" : "does not act"}
                    {run.gate.reason ? <span className="cell-sub">{run.gate.reason}</span> : null}
                  </>
                ) : (
                  <span className="muted">not reached</span>
                )}
              </dd>
            </div>
            <div className="run-cell">
              <dt>Broker config</dt>
              <dd>
                {run.broker_id ? <span className="mono">{run.broker_id}</span> : <span className="muted">unknown broker</span>}
                {run.sop_version ? <span className="cell-sub">SOP {run.sop_version.slice(0, 12)}</span> : null}
              </dd>
            </div>
            <div className="run-cell">
              <dt>SOP sections</dt>
              <dd className="tag-list">
                {run.sop_section_ids.length ? (
                  run.sop_section_ids.map((id) => (
                    <Tag key={id} tone="sop">
                      {id}
                    </Tag>
                  ))
                ) : (
                  <span className="muted">none</span>
                )}
              </dd>
            </div>
            <div className="run-cell">
              <dt>Agent loop</dt>
              <dd>
                {trajectory.length ? (
                  <>
                    {tally.steps} tool call{tally.steps === 1 ? "" : "s"}: {tally.accepted} accepted, {tally.rejected} rejected, {tally.reads} read
                    {tally.reads === 1 ? "" : "s"}
                    <span className={clsx("cell-sub", (run.rejections ?? 0) > 0 && "text-warn")}>
                      {run.rejections ?? 0} rejection{run.rejections === 1 ? "" : "s"} counted against the budget
                    </span>
                  </>
                ) : run.plan ? (
                  <span className="muted">no tool call: the plan came in the answer</span>
                ) : (
                  <span className="muted">not reached</span>
                )}
              </dd>
            </div>
            {searched.length ? (
              <div className="run-cell">
                <dt>Sections searched</dt>
                <dd className="tag-list">
                  {searched.map((id) => (
                    <Tag key={id} tone="sop">
                      {id}
                    </Tag>
                  ))}
                </dd>
              </div>
            ) : null}
            <div className="run-cell">
              <dt>Tokens</dt>
              <dd>
                {tokens.input.toLocaleString("en-US")} in, {tokens.output.toLocaleString("en-US")} out
                {tokens.cached ? `, ${tokens.cached.toLocaleString("en-US")} cached` : ""}
              </dd>
            </div>
            <div className="run-cell">
              <dt>Cost and time</dt>
              <dd>
                {usd(cost)}, {formatDuration(runLatencyMs(run) / 1000)} in model calls
                {duration !== null ? <span className="cell-sub">run took {formatDuration(duration)}</span> : null}
              </dd>
            </div>
          </dl>

          {run.usage.length ? (
            <div className="table-wrap usage-wrap">
            <table className="table table--compact usage-table">
              <caption className="visually-hidden">Model calls</caption>
              <thead>
                <tr>
                  <th scope="col">Role</th>
                  <th scope="col">Model</th>
                  <th scope="col" className="num">In</th>
                  <th scope="col" className="num">Out</th>
                  <th scope="col" className="num">Latency</th>
                  <th scope="col" className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {run.usage.map((u, i) => (
                  <tr key={i}>
                    <td>{u.role}</td>
                    <td className="mono">{u.model}</td>
                    <td className="num">{u.input_tokens}</td>
                    <td className="num">{u.output_tokens}</td>
                    <td className="num">{u.latency_ms} ms</td>
                    <td className="num">{usd(u.cost_usd, 5)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          ) : null}

          {trajectory.length ? <Trajectory steps={trajectory} /> : null}

          <h4 className="subhead">{trajectory.length ? "Staged plan" : "Plan"}</h4>
          {run.plan && run.plan.actions.length ? (
            <ol className="plan">
              {run.plan.actions.map((action, i) => {
                const violations = dropped.get(i);
                return (
                  <li key={i} className={clsx("plan-step", violations && "plan-step--dropped")}>
                    <ActionLine action={action} />
                    {violations?.map((v, j) => (
                      <p key={j} className={clsx("violation", `violation--${v.severity}`)}>
                        Dropped by <span className="mono">{v.rule}</span>
                        {v.severity === "high" ? " (high severity)" : ""}: {v.message}
                      </p>
                    ))}
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="muted">{run.plan ? "Empty plan: the agent chose to do nothing." : "No plan: the run ended before the decide step."}</p>
          )}
          {run.violations.filter((v) => !run.plan || v.action_index >= run.plan.actions.length).map((v, i) => (
            <p key={`extra-${i}`} className={clsx("violation", `violation--${v.severity}`)}>
              <span className="mono">{v.rule}</span>: {v.message}
            </p>
          ))}
          {run.plan?.rationale ? (
            <p className="rationale">
              <span className="strong">Model's rationale: </span>
              {run.plan.rationale}
            </p>
          ) : null}

          {run.skip_reason || run.error ? (
            <p className={clsx("outcome", run.status === "failed" && "outcome--failed")}>
              <span className="strong">
                {run.status === "skipped" ? "Skipped" : run.status === "escalated" ? "Escalated" : run.status === "failed" ? "Failed" : "Reason"}:
              </span>{" "}
              {[run.skip_reason, run.error].filter(Boolean).join("; ")}
            </p>
          ) : null}
          {run.trace_url ? (
            <p>
              <a href={run.trace_url} target="_blank" rel="noreferrer">
                Open the trace
              </a>
            </p>
          ) : null}
          <p className="run-id">
            <span className="mono">{run.run_id}</span>
          </p>
        </div>
      ) : null}
    </li>
  );
}

const VERDICT_LABEL = { read: "Read", accepted: "Accepted", rejected: "Rejected" } as const;
const VERDICT_CHIP = { read: "chip--neutral", accepted: "chip--pass", rejected: "chip--fail" } as const;

/** Every tool call of the loop, in order: what the model asked and what it was told. */
function Trajectory({ steps }: { steps: readonly TrajectoryStep[] }) {
  return (
    <>
      <h4 className="subhead">Trajectory</h4>
      <div className="table-wrap usage-wrap">
        <table className="table table--compact trajectory">
          <caption className="visually-hidden">Tool calls of the agent loop, in order</caption>
          <thead>
            <tr>
              <th scope="col" className="num">Step</th>
              <th scope="col">Tool</th>
              <th scope="col">Arguments</th>
              <th scope="col">Result</th>
              <th scope="col">Rules</th>
              <th scope="col" className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((s, i) => {
              const verdict = stepVerdict(s);
              const rule = verdict === "rejected" ? rejectionRule(s.result) : null;
              const args = compactArgs(s.args);
              return (
                <tr key={i} className={clsx(verdict === "rejected" && "row--fail")}>
                  <td className="num">{i + 1}</td>
                  <td className="mono">{s.tool}</td>
                  <td className="mono trajectory-args" title={JSON.stringify(s.args, null, 2)}>
                    {clip(args, 160)}
                  </td>
                  <td className="trajectory-result" title={s.result}>
                    {clip(s.result, 180)}
                  </td>
                  <td>
                    <span className={clsx("chip", VERDICT_CHIP[verdict])}>{s.tool === "finish" && verdict === "read" ? "Answer" : VERDICT_LABEL[verdict]}</span>
                    {rule ? <span className="cell-sub mono">{rule}</span> : null}
                  </td>
                  <td className="num">{usd(s.cost_usd, 5)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ActionLine({ action }: { action: Action }) {
  switch (action.type) {
    case "send_sms":
      return (
        <p className="action">
          <span className="action-kind">Text</span> <span className="mono">{action.contact_id}</span>
          <Tag tone="muted">{humanize(action.purpose)}</Tag>
          {action.requests && action.requests !== action.purpose ? <Tag tone="muted">requests {humanize(action.requests)}</Tag> : null}
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "send_email":
      return (
        <p className="action">
          <span className="action-kind">Email</span> <span className="mono">{action.to_contact_ids.join(", ")}</span>
          <Tag tone="muted">{humanize(action.purpose)}</Tag>
          {action.requests && action.requests !== action.purpose ? <Tag tone="muted">requests {humanize(action.requests)}</Tag> : null}
          {action.subject ? <span className="action-text strong">{action.subject}</span> : null}
          <span className="action-text">{action.body}</span>
        </p>
      );
    case "post_broker_slack":
      return (
        <p className="action">
          <span className="action-kind">Broker Slack</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "write_tms_note":
      return (
        <p className="action">
          <span className="action-kind">TMS note</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "create_hero_task":
      return (
        <p className="action">
          <span className="action-kind">Hero task</span>
          {action.urgent ? <Tag tone="urgent">Urgent</Tag> : null}
          <span className="action-text strong">{action.title}</span>
          <span className="action-text">{action.description}</span>
        </p>
      );
    case "post_internal_slack":
      return (
        <p className="action">
          <span className="action-kind">Internal Slack</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "update_stage":
      return (
        <p className="action">
          <span className="action-kind">Stage</span> {stageLabel(action.stage)}
        </p>
      );
    case "schedule_timer":
      return (
        <p className="action">
          <span className="action-kind">Timer</span> follow up on the {humanize(action.kind).toLowerCase()} in {action.due_in_minutes} min
        </p>
      );
    case "defer_obligation":
      return (
        <p className="action">
          <span className="action-kind">Defer</span> {humanize(action.kind).toLowerCase()} by {action.minutes} min
        </p>
      );
    case "close_obligation":
      return (
        <p className="action">
          <span className="action-kind">Close</span> {humanize(action.kind)} as {action.status}
        </p>
      );
    case "open_obligation":
      return (
        <p className="action">
          <span className="action-kind">Open</span> {humanize(action.kind)}
        </p>
      );
    default: {
      const unknown = action as { type: string };
      return (
        <p className="action">
          <span className="action-kind">{humanize(unknown.type)}</span>
          <span className="action-text mono">{JSON.stringify(unknown)}</span>
        </p>
      );
    }
  }
}

function BudgetPanel() {
  const { loads, selectedLoadId } = useSim();
  return (
    <Panel title="Model spend per load">
      {loads.length === 0 ? (
        <Empty title="No load yet" />
      ) : (
        <div className="stack">
          {[...loads]
            .sort((a, b) => b.cost_usd - a.cost_usd)
            .map((l) => (
              <Meter
                key={l.load_id}
                label={l.load_id === selectedLoadId ? `${l.load_id} (selected)` : l.load_id}
                value={l.cost_usd}
                max={BUDGET_USD}
                warnAt={0.8}
                valueText={`${usd(l.cost_usd)} of $1.00`}
              />
            ))}
          <p className="hint">The budget is $1 of model calls for the whole life of a load.</p>
        </div>
      )}
    </Panel>
  );
}
