import clsx from "clsx";

import { runCost, runLatencyMs, runTokens, type OutboxItem, type RunRecord, type TrajectoryStep } from "../api";
import { humanize } from "../lib/load";
import { formatDuration } from "../lib/time";
import { groupActions, reviewPlan } from "../lib/timeline";
import { clip, compactArgs, rejectionRule, stepVerdict, tallyTrajectory } from "../lib/trajectory";
import { ActionGroups, ActionLine, DroppedActions } from "./Actions";
import { RunGraph } from "./RunGraph";
import { Tag, usd } from "./ui";

/** Everything one run saw, decided and did, under its line in the timeline. */
export function RunDetail({ run, outbox }: { run: RunRecord; outbox: readonly OutboxItem[] }) {
  const trajectory = run.trajectory ?? [];
  const groups = groupActions(outbox, run.run_id);
  const { ledger, dropped } = reviewPlan(run);
  const outcome = [run.skip_reason, run.error].filter(Boolean).join("; ");

  return (
    <div className="run-body">
      <div className="run-top">
        <RunFacts run={run} />
        <RunGraph run={run} />
      </div>

      <h4 className="subhead">Trajectory</h4>
      {trajectory.length ? (
        <Trajectory steps={trajectory} />
      ) : (
        <p className="muted">{run.plan ? "No tool call: the plan came in the answer." : "The run ended before the agent."}</p>
      )}

      <h4 className="subhead">Answer</h4>
      {run.plan?.rationale ? <p className="answer">{run.plan.rationale}</p> : <p className="muted">No answer: the run made no plan.</p>}
      {outcome ? (
        <p className={clsx("outcome", run.status === "failed" && "outcome--failed")}>
          <span className="strong">{run.status === "skipped" ? "Skipped" : run.status === "failed" ? "Failed" : "Note"}:</span> {outcome}
        </p>
      ) : null}

      <h4 className="subhead">Actions</h4>
      {groups.length ? <ActionGroups groups={groups} /> : <p className="muted">Nothing sent.</p>}
      {ledger.length ? (
        <section className="action-group">
          <h5 className="action-group-title">Ledger</h5>
          <ul className="action-list">
            {ledger.map((action, i) => (
              <li key={i}>
                <ActionLine action={action} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {dropped.length ? (
        <>
          <h4 className="subhead">Dropped by the validator</h4>
          <DroppedActions dropped={dropped} />
        </>
      ) : null}

      {run.usage.length ? <ModelCalls run={run} /> : null}

      <p className="run-id">
        {run.trace_url ? (
          <a href={run.trace_url} target="_blank" rel="noreferrer">
            Open the trace in LangSmith
          </a>
        ) : (
          <span>No trace: LangSmith tracing is off.</span>
        )}{" "}
        <span className="mono">{run.run_id}</span>
      </p>
    </div>
  );
}

function RunFacts({ run }: { run: RunRecord }) {
  const tokens = runTokens(run);
  const cost = runCost(run);
  const tally = tallyTrajectory(run.trajectory ?? []);
  const intent = run.intent ?? run.gate?.intent ?? null;
  return (
    <dl className="run-grid">
      <div className="run-cell">
        <dt>Intent</dt>
        <dd>
          {intent ? (
            <>
              <span className="strong">{humanize(intent)}</span> <span className="mono muted">{intent}</span>
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
              {humanize(run.gate.intent)}, {run.gate.act ? "acts" : "does not act"}
              {run.gate.reason ? <span className="cell-sub">{run.gate.reason}</span> : null}
            </>
          ) : (
            <span className="muted">not reached</span>
          )}
        </dd>
      </div>
      <div className="run-cell">
        <dt>Tool calls</dt>
        <dd>
          {tally.steps ? (
            <>
              {tally.steps}: {tally.accepted} accepted, {tally.rejected} refused, {tally.reads} read{tally.reads === 1 ? "" : "s"}
            </>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
      </div>
      <div className="run-cell">
        <dt>Cost and time</dt>
        <dd>
          {usd(cost)}, {formatDuration(runLatencyMs(run) / 1000)} in model calls
          <span className="cell-sub">
            {tokens.input.toLocaleString("en-US")} tokens in, {tokens.output.toLocaleString("en-US")} out
            {tokens.cached ? `, ${tokens.cached.toLocaleString("en-US")} cached` : ""}
          </span>
        </dd>
      </div>
      <div className="run-cell run-cell--wide">
        <dt>SOP sections</dt>
        <dd className="tag-list">
          {run.sop_section_ids.length ? run.sop_section_ids.map((id) => <Tag key={id} tone="sop">{id}</Tag>) : <span className="muted">none</span>}
          {(run.sections_searched ?? []).filter((id) => !run.sop_section_ids.includes(id)).map((id) => (
            <Tag key={id} tone="muted">
              searched {id}
            </Tag>
          ))}
        </dd>
      </div>
      <div className="run-cell run-cell--wide">
        <dt>Facility notes shown</dt>
        <dd>
          {run.facility_facts?.length ? (
            <ul className="note-list">
              {run.facility_facts.map((fact) => (
                <li key={fact.fact_id}>
                  {fact.facility_name}: <q>{fact.text}</q>{" "}
                  <span className="muted">(load {fact.source_load_id})</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
      </div>
    </dl>
  );
}

const VERDICT_LABEL = { read: "Read", accepted: "Accepted", rejected: "Refused" } as const;
const VERDICT_CHIP = { read: "chip--neutral", accepted: "chip--pass", rejected: "chip--fail" } as const;

/** Every tool call of the agent loop, in order: what the model asked and what it was told. */
function Trajectory({ steps }: { steps: readonly TrajectoryStep[] }) {
  return (
    <div className="table-wrap usage-wrap">
      <table className="table table--compact trajectory">
        <caption className="visually-hidden">Tool calls of the agent loop, in order</caption>
        <thead>
          <tr>
            <th scope="col" className="num">#</th>
            <th scope="col">Tool</th>
            <th scope="col">Arguments</th>
            <th scope="col">Result</th>
            <th scope="col">Rules</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((s, i) => {
            const verdict = stepVerdict(s);
            const rule = verdict === "rejected" ? rejectionRule(s.result) : null;
            return (
              <tr key={i} className={clsx(verdict === "rejected" && "row--fail")}>
                <td className="num">{i + 1}</td>
                <td className="mono">{s.tool}</td>
                <td className="mono trajectory-args" title={JSON.stringify(s.args, null, 2)}>
                  {clip(compactArgs(s.args), 200)}
                </td>
                <td className="trajectory-result" title={s.result}>
                  {clip(s.result, 220)}
                </td>
                <td>
                  <span className={clsx("chip", VERDICT_CHIP[verdict])}>{VERDICT_LABEL[verdict]}</span>
                  {rule ? <span className="cell-sub mono">{rule}</span> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ModelCalls({ run }: { run: RunRecord }) {
  return (
    <details className="model-calls">
      <summary>
        {run.usage.length} model call{run.usage.length === 1 ? "" : "s"}
      </summary>
      <div className="table-wrap usage-wrap">
        <table className="table table--compact">
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
    </details>
  );
}
