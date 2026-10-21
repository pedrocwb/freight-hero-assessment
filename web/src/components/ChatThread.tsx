import clsx from "clsx";
import { Fragment, useEffect, useRef } from "react";

import type { ChatItem, ChatMarker, ChatMessage } from "../lib/conversation";
import type { FlowState } from "../lib/sendFlow";
import { clockTime, formatDay } from "../lib/time";
import { clip } from "../lib/trajectory";

/**
 * The load's conversation: people on the left, the agent on the right, the quiet markers
 * between. An agent message or marker opens "why" for its run; the selected run's items
 * are outlined. While an input waits, it shows at the bottom with "the agent is thinking".
 */
export function ChatThread(props: {
  items: readonly ChatItem[];
  timezone: string;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  flow: FlowState;
  emptyHint: string;
}) {
  const { items, timezone, flow } = props;
  const end = useRef<HTMLDivElement>(null);
  const last = items.at(-1)?.key;

  // Follow the conversation as it grows, and while the agent thinks.
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [last, flow.phase]);

  let day = "";
  return (
    <div className="thread" role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation">
      {!items.length && flow.phase !== "waiting" ? <p className="thread-empty">{props.emptyHint}</p> : null}
      {items.map((item) => {
        const itemDay = formatDay(item.at, timezone);
        const separator = itemDay !== day ? itemDay : null;
        day = itemDay;
        return (
          <Fragment key={item.key}>
            {separator ? <p className="day">{separator}</p> : null}
            {item.kind === "message" ? (
              <Bubble message={item} timezone={timezone} selected={item.side === "out" && item.runId === props.selectedRunId} onSelect={props.onSelect} />
            ) : (
              <Marker marker={item} timezone={timezone} selected={item.runId !== null && item.runId === props.selectedRunId} onSelect={props.onSelect} />
            )}
          </Fragment>
        );
      })}
      {flow.phase === "waiting" ? (
        <>
          {flow.pending ? (
            <div className="bubble-row bubble-row--in">
              <div className="bubble bubble--in bubble--pending">
                <p className="bubble-meta">
                  <span className="bubble-from">{flow.pending.from}</span>
                  <span>{flow.pending.channel}</span>
                  <span>sending</span>
                </p>
                {flow.pending.body ? <p className="bubble-body">{flow.pending.body}</p> : null}
                {flow.pending.attachments.map((name) => (
                  <p key={name} className="bubble-file">{name}</p>
                ))}
              </div>
            </div>
          ) : null}
          <div className="thinking" role="status">
            <span className="thinking-dots" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
            {flow.label}
          </div>
        </>
      ) : null}
      <div ref={end} />
    </div>
  );
}

function Bubble({ message, timezone, selected, onSelect }: { message: ChatMessage; timezone: string; selected: boolean; onSelect: (runId: string) => void }) {
  const out = message.side === "out";
  const body = (
    <>
      <p className="bubble-meta">
        <span className="bubble-from">{out ? `To ${message.to ?? "someone"}` : message.from}</span>
        <span>{message.channel}</span>
        <time dateTime={message.at}>{clockTime(message.at, timezone)}</time>
      </p>
      {message.subject ? <p className="bubble-subject">{message.subject}</p> : null}
      {message.body ? <p className="bubble-body">{message.body}</p> : null}
      {message.attachments.map((name) => (
        <p key={name} className="bubble-file">
          {name}
        </p>
      ))}
      {out && (message.purpose || message.delivery !== "sent") ? (
        <p className="bubble-foot">
          {message.purpose ? <span>{message.purpose}</span> : null}
          {message.delivery && message.delivery !== "sent" ? <span className={clsx("delivery", `delivery--${message.delivery}`)}>{message.delivery === "failed" ? "Not delivered" : "Sending"}</span> : null}
        </p>
      ) : null}
    </>
  );
  return (
    <div className={clsx("bubble-row", out ? "bubble-row--out" : "bubble-row--in")}>
      {out && message.runId ? (
        <button
          type="button"
          className={clsx("bubble bubble--out", selected && "bubble--selected")}
          aria-pressed={selected}
          aria-label={`Why the agent wrote to ${message.to}: ${message.body.slice(0, 80)}`}
          onClick={() => onSelect(message.runId!)}
        >
          {body}
        </button>
      ) : (
        <div className={clsx("bubble", out ? "bubble--out" : "bubble--in")}>{body}</div>
      )}
    </div>
  );
}

function Marker({ marker, timezone, selected, onSelect }: { marker: ChatMarker; timezone: string; selected: boolean; onSelect: (runId: string) => void }) {
  const content = (
    <>
      <span className="marker-line">
        <span className="marker-text">{marker.text}</span>
        <time className="marker-time" dateTime={marker.at}>
          {clockTime(marker.at, timezone)}
        </time>
      </span>
      {marker.detail ? <span className="marker-detail">{clip(marker.detail, 140)}</span> : null}
    </>
  );
  const className = clsx("marker", `marker--${marker.tone}`, selected && "marker--selected");
  return marker.runId ? (
    <button type="button" className={className} aria-pressed={selected} onClick={() => onSelect(marker.runId!)} title="Why?">
      {content}
    </button>
  ) : (
    <p className={className}>{content}</p>
  );
}
