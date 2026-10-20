import clsx from "clsx";
import { useEffect, useRef } from "react";

import { Composer } from "../components/Composer";
import { Empty, InlineError, NoLoad, ScreenHead, Tag } from "../components/ui";
import { useThread } from "../hooks/useThread";
import { groupByDay, initials } from "../lib/conversation";
import { contactFor } from "../lib/load";
import { formatTime } from "../lib/time";
import { useSim } from "../state/sim";

export function BrokerSlack() {
  const sim = useSim();
  const { load, timezone } = sim;
  const broker = contactFor(load, "broker", "slack");
  const onSlack = broker?.channels.includes("slack") ?? false;
  const { resource, entries, send, attach, discard } = useThread("slack");
  const thread = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = thread.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries.length]);

  if (!sim.selectedLoadId) {
    return (
      <>
        <ScreenHead title="Broker Slack" />
        <NoLoad what="the broker's Slack channel" />
      </>
    );
  }

  return (
    <>
      <ScreenHead
        title="Broker Slack"
        lede={
          broker ? (
            <>
              You are the broker <span className="mono">{broker.id}</span> at {load?.broker_name}. The agent alerts this
              channel when the client's procedure says so.
            </>
          ) : (
            "This load has no broker contact."
          )
        }
      />
      <div className="slack">
        <div className="slack-head">
          <p className="slack-channel">{load?.broker_name ?? "Broker"} channel</p>
          <p className="slack-topic">Messages about load {sim.selectedLoadId}</p>
        </div>
        <div className="slack-thread" role="log" aria-label="Slack channel" aria-live="polite" ref={thread} tabIndex={0}>
          {resource.error ? <InlineError message={resource.error} onRetry={() => void resource.reload()} /> : null}
          {resource.data && entries.length === 0 ? (
            <Empty title="No message in the channel yet">
              Keystone's high-risk procedure alerts the broker here when the driver gives no ETA in 30
              minutes; Northline's does not. You can also write as the broker below.
            </Empty>
          ) : null}
          {groupByDay(entries, timezone).map((group) => (
            <div key={group.key} className="day">
              <p className="day-label day-label--rule">{group.label}</p>
              {group.items.map((entry) => {
                const agent = entry.who === "agent";
                return (
                  <div key={entry.key} className={clsx("slack-msg", entry.pending === "failed" && "slack-msg--failed")}>
                    <span className={clsx("avatar avatar--square", `avatar--${agent ? "agent" : entry.role}`)} aria-hidden="true">
                      {initials(entry.who, entry.role)}
                    </span>
                    <div>
                      <p className="slack-meta">
                        <strong>{agent ? "Load agent" : entry.who}</strong>
                        {agent ? <Tag tone="muted">App</Tag> : null}
                        <time dateTime={entry.at}>{formatTime(entry.at, timezone)}</time>
                        {entry.pending === "sending" ? <span className="muted">sending</span> : null}
                      </p>
                      <p className="slack-text">{entry.text || <span className="muted">Shared a file</span>}</p>
                      {entry.pending === "failed" ? (
                        <p className="field-error">
                          Not sent: {entry.error}{" "}
                          <button type="button" className="linklike" onClick={() => discard(entry.key)}>
                            Remove
                          </button>
                        </p>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        <Composer
          className="slack-composer"
          label="Slack message as the broker"
          placeholder={broker ? `Message as ${broker.id}` : "No broker on this load"}
          sendLabel="Send"
          disabled={!broker || !onSlack || !sim.state}
          disabledReason={broker ? `${broker.id} is not on Slack for this load` : "No broker on this load"}
          suggestions={["Any update on the pickup ETA?", "Thanks"]}
          onSend={(text) => (broker ? send(broker, text) : Promise.resolve(false))}
          onAttach={(file, text) => (broker ? attach(broker, file, text) : Promise.resolve(null))}
        />
      </div>
    </>
  );
}
