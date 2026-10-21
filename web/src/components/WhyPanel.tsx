import clsx from "clsx";
import { useState } from "react";

import { runCost, runLatencyMs, runTokens, type Load, type LoadEvent, type RecordedStep, type RunRecord } from "../api";
import { describePath } from "../lib/graph";
import { formatDuration, formatTime } from "../lib/time";
import { clip, compactArgs } from "../lib/trajectory";
import { explainRun, type SectionInfo } from "../lib/why";
import { RunGraph } from "./RunGraph";
import { percent, usd } from "./ui";

/** "Why did the agent do this?": one run, short and in plain words, the machinery folded away. */
export function WhyPanel(props: {
  run: RunRecord;
  event: LoadEvent | null;
  load: Load;
  sections: ReadonlyMap<string, SectionInfo>;
  step: RecordedStep | null;
  timezone: string;
  onClose: () => void;
}) {
  const { run } = props;
  const why = explainRun(run, { event: props.event, load: props.load, sections: props.sections, step: props.step });

  return (
    <aside className="why" aria-labelledby="why-title">
      <header className="why-head">
        <div>
          <p className="why-kicker">
            Run at <time dateTime={run.started_at}>{formatTime(run.started_at, props.timezone)}</time>
          </p>
          <h2 className="why-title" id="why-title">
            {why.headline}
          </h2>
        </div>
        <button type="button" className="icon-button" onClick={props.onClose} aria-label="Close the explanation">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="m5 5 10 10M15 5 5 15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      {why.rationale ? <blockquote className="why-quote">{why.rationale}</blockquote> : null}

      <section className="why-section">
        <h3 className="why-label">Understood</h3>
        <p>{why.understood.event}.</p>
        {why.understood.intent ? (
          <p className="why-sub">
            Read as <strong>{why.understood.intent}</strong>
            {why.understood.intentProbability !== null ? ` (${percent(why.understood.intentProbability)})` : ""}
          </p>
        ) : null}
        {why.understood.gate ? (
          <p className="why-sub">
            {why.understood.gate}
            {why.understood.actProbability !== null ? `, ${percent(why.understood.actProbability)} sure it should act now` : ""}
          </p>
        ) : null}
      </section>

      {why.followed.length || why.notes.length ? (
        <section className="why-section">
          <h3 className="why-label">Followed</h3>
          <ul className="why-list">
            {why.followed.map((s) => (
              <li key={s.id}>
                {s.title}
                {s.layer ? <span className="why-sub"> ({s.layer})</span> : null}
              </li>
            ))}
          </ul>
          {why.notes.length ? (
            <>
              <p className="why-sub">Facility notes it saw:</p>
              <ul className="why-list">
                {why.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
      ) : null}

      <section className="why-section">
        <h3 className="why-label">Did</h3>
        {why.did.length ? (
          <ol className="why-did">
            {why.did.map((line, i) => (
              <li key={i} className={clsx("did", line.ok ? "did--ok" : "did--refused")}>
                <span className="did-mark" aria-hidden="true">
                  {line.ok ? "✓" : "✕"}
                </span>
                <span>
                  <span className="visually-hidden">{line.ok ? "Done: " : "Refused: "}</span>
                  <span className="did-text">{line.text}</span>
                  {line.reason ? <span className="did-reason">Refused. {line.reason}</span> : null}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="why-sub">Nothing.</p>
        )}
      </section>

      {why.leftOpen ? (
        <section className="why-section">
          <h3 className="why-label">Left open</h3>
          <ul className="why-list">
            {why.leftOpen.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <Details run={run} />
    </aside>
  );
}

function Details({ run }: { run: RunRecord }) {
  const [open, setOpen] = useState(false);
  const tokens = runTokens(run);
  const steps = run.trajectory ?? [];
  return (
    <details className="details" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>Details</summary>
      <dl className="facts">
        <div>
          <dt>Path</dt>
          <dd>{run.nodes?.length ? describePath(run.nodes) : "none"}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>
            {run.usage.length}, {usd(runCost(run))}, {formatDuration(runLatencyMs(run) / 1000)}
          </dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd>
            {tokens.input.toLocaleString("en-US")} in, {tokens.output.toLocaleString("en-US")} out
            {tokens.cached ? `, ${tokens.cached.toLocaleString("en-US")} cached` : ""}
          </dd>
        </div>
        {run.gate_engine ? (
          <div>
            <dt>Gate</dt>
            <dd>{run.gate_engine === "jev" ? "decision model" : "chat model"}</dd>
          </div>
        ) : null}
        <div>
          <dt>Run</dt>
          <dd className="mono">{run.run_id}</dd>
        </div>
      </dl>
      {run.trace_url ? (
        <p>
          <a href={run.trace_url} target="_blank" rel="noreferrer">
            Open the trace in LangSmith
          </a>
        </p>
      ) : (
        <p className="why-sub">No trace: LangSmith tracing is off.</p>
      )}
      {steps.length ? (
        <>
          <h4 className="details-head">Tool calls</h4>
          <ol className="calls">
            {steps.map((s, i) => (
              <li key={i} className={clsx(s.accepted === false && "calls-refused")}>
                <code>{s.tool}</code> <span className="mono calls-args">{clip(compactArgs(s.args), 160)}</span>
                <span className="calls-result">{clip(s.result, 200)}</span>
              </li>
            ))}
          </ol>
        </>
      ) : null}
      {run.usage.length ? (
        <>
          <h4 className="details-head">Model calls</h4>
          <ul className="calls">
            {run.usage.map((u, i) => (
              <li key={i}>
                <code>{u.role}</code> <span className="mono calls-args">{u.model}</span>
                <span className="calls-result">
                  {u.input_tokens} in, {u.output_tokens} out, {u.latency_ms} ms, {usd(u.cost_usd, 5)}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {open ? (
        <>
          <h4 className="details-head">Run graph</h4>
          <RunGraph run={run} />
        </>
      ) : null}
    </details>
  );
}
