import clsx from "clsx";

import { runCost, type LoadSnapshot } from "../api";
import { describeEvent, eventKind, type EventKind, type TimelineEntry, type TimelineRun } from "../lib/timeline";
import { formatTime, formatTimeSeconds, groupByDay } from "../lib/time";
import { useSim } from "../state/sim";
import { RunDetail } from "./RunDetail";
import { StatusChip, usd } from "./ui";

const KIND_LABEL: Record<EventKind, string> = {
  message: "Message",
  ping: "Ping",
  timer: "Timer",
  document: "Document",
  hero: "Hero",
};

/**
 * The load's story in load time: each inbound event, then the run it caused. One run is
 * open at a time; it is the run the graph and the trajectory are about.
 */
export function Timeline(props: {
  entries: readonly TimelineEntry[];
  snapshot: LoadSnapshot;
  openRunId: string | null;
  onToggle: (runId: string) => void;
}) {
  const { timezone } = useSim();
  return (
    <div className="timeline">
      {groupByDay(props.entries, timezone).map((day) => (
        <section key={day.key} className="day" aria-label={day.label}>
          <p className="day-label">{day.label}</p>
          <ol className="tl">
            {day.items.map((entry) => (
              <EntryItem key={entry.key} entry={entry} {...props} />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}

function EntryItem(props: {
  entry: TimelineEntry;
  snapshot: LoadSnapshot;
  openRunId: string | null;
  onToggle: (runId: string) => void;
}) {
  const { entry, snapshot } = props;
  const { timezone } = useSim();
  const kind = entry.event ? eventKind(entry.event) : "message";
  return (
    <li className={clsx("tl-entry", `tl-entry--${kind}`)}>
      <time className="tl-time" dateTime={entry.at}>
        {formatTime(entry.at, timezone)}
      </time>
      <div className="tl-body">
        <p className="tl-event">
          <span className="tl-kind">{entry.event ? KIND_LABEL[kind] : "Run"}</span>
          {describeEvent(entry.event, snapshot.load)}
        </p>
        <EventBody entry={entry} />
        {entry.runs.length ? (
          <ol className="runs">
            {entry.runs.map((numbered) => (
              <RunCard
                key={numbered.run.run_id}
                numbered={numbered}
                snapshot={snapshot}
                open={props.openRunId === numbered.run.run_id}
                onToggle={() => props.onToggle(numbered.run.run_id)}
              />
            ))}
          </ol>
        ) : (
          <p className="tl-none">Queued: the run follows after the inbound latency. Move the clock or tick.</p>
        )}
      </div>
    </li>
  );
}

function EventBody({ entry }: { entry: TimelineEntry }) {
  const event = entry.event;
  if (!event) return null;
  return (
    <>
      {event.body ? <blockquote className="quote">{event.body}</blockquote> : null}
      {event.attachments.length ? (
        <p className="cell-sub">
          {event.attachments.map((a) => (
            <span key={a.attachment_id} className="mono">
              {a.filename}{" "}
            </span>
          ))}
        </p>
      ) : null}
    </>
  );
}

function RunCard(props: { numbered: TimelineRun; snapshot: LoadSnapshot; open: boolean; onToggle: () => void }) {
  const { run, number } = props.numbered;
  const { timezone } = useSim();
  const bodyId = `run-${run.run_id}`;
  const intent = run.intent ?? run.gate?.intent ?? null;
  const calls = run.trajectory?.length ?? 0;
  return (
    <li className={clsx("run", `run--${run.status}`, props.open && "run--open")} id={`run-card-${run.run_id}`}>
      <h3 className="run-head">
        <button type="button" className="run-toggle" aria-expanded={props.open} aria-controls={bodyId} onClick={props.onToggle}>
          <span className="run-num">Run {number}</span>
          <time className="run-time" dateTime={run.started_at}>
            {formatTimeSeconds(run.started_at, timezone)}
          </time>
          <StatusChip status={run.status} />
          <span className="run-trigger">
            {intent ? <span className="mono">{intent}</span> : <span className="muted">no intent</span>}
            {calls ? <span className="run-intent">{calls} tool call{calls === 1 ? "" : "s"}</span> : null}
          </span>
          <span className="run-cost">{usd(runCost(run))}</span>
          <span className="run-caret" aria-hidden="true" />
        </button>
      </h3>
      {props.open ? (
        <div id={bodyId}>
          <RunDetail run={run} outbox={props.snapshot.outbox} />
        </div>
      ) : null}
    </li>
  );
}
