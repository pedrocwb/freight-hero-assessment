import clsx from "clsx";
import { useEffect, useState } from "react";

import { Composer } from "../components/Composer";
import { Empty, InlineError, NoLoad, Panel, ScreenHead, Tag } from "../components/ui";
import { useThread } from "../hooks/useThread";
import { emailRecipients, initials } from "../lib/conversation";
import { roleLabel } from "../lib/load";
import { formatStamp } from "../lib/time";
import { useSim } from "../state/sim";

export function EmailThread() {
  const sim = useSim();
  const { load, timezone, state } = sim;
  const { resource, entries, send, attach, discard } = useThread("email");
  const writers = (load?.contacts ?? []).filter((c) => c.channels.includes("email"));
  const [writerId, setWriterId] = useState<string>("");
  const writer = writers.find((c) => c.id === writerId) ?? writers[0];

  useEffect(() => {
    if (writer && writer.id !== writerId) setWriterId(writer.id);
  }, [writer, writerId]);

  if (!sim.selectedLoadId) {
    return (
      <>
        <ScreenHead title="Main email thread" />
        <NoLoad what="the load's email thread" />
      </>
    );
  }

  const who = (id: string, role: string) => (id === "agent" ? `${load?.broker_name ?? "Broker"} (agent)` : `${roleLabel(role)} ${id}`);
  const outbox = sim.snapshot.data?.outbox ?? [];

  return (
    <>
      <ScreenHead
        title="Main email thread"
        lede="The load's one thread. The dispatcher, the broker and the agent all write here; the agent writes as the broker's team."
      />
      <div className="grid grid--thread">
        <Panel title={`Load ${sim.selectedLoadId}, main thread`} flush>
          {resource.error ? <InlineError message={resource.error} onRetry={() => void resource.reload()} /> : null}
          {resource.data && entries.length === 0 ? (
            <Empty title="No email yet">
              Write as the dispatcher or the broker below. The agent emails the dispatcher when
              the client's procedure calls for it, for example after two unanswered ETA texts.
            </Empty>
          ) : null}
          <ol className="mail-list" aria-label="Messages, oldest first">
            {entries.map((entry) => {
              const to = emailRecipients(entry, outbox);
              return (
                <li key={entry.key} className={clsx("mail", entry.who === "agent" && "mail--agent")}>
                  <span className={clsx("avatar", `avatar--${entry.who === "agent" ? "agent" : entry.role}`)} aria-hidden="true">
                    {initials(entry.who, entry.role)}
                  </span>
                  <div className="mail-main">
                    <div className="mail-head">
                      <p className="mail-from">
                        {who(entry.who, entry.role)}
                        {entry.who === "agent" ? <Tag tone="agent">Agent</Tag> : null}
                      </p>
                      <time className="mail-time" dateTime={entry.at}>
                        {formatStamp(entry.at, timezone, state?.now)}
                      </time>
                    </div>
                    {to.length ? <p className="mail-to">To {to.map((id) => who(id, load?.contacts.find((c) => c.id === id)?.role ?? "")).join(", ")}</p> : null}
                    {entry.subject ? <p className="mail-subject">{entry.subject}</p> : null}
                    <p className="mail-body">{entry.text || <span className="muted">Attachment, no text</span>}</p>
                    {entry.pending ? (
                      <p className="mail-state">
                        {entry.pending === "sending" ? "Sending…" : entry.pending === "sent" ? "Delivered to the agent" : `Not sent: ${entry.error}`}
                        {entry.pending === "failed" ? (
                          <>
                            {" "}
                            <button type="button" className="linklike" onClick={() => discard(entry.key)}>
                              Remove
                            </button>
                          </>
                        ) : null}
                      </p>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </Panel>

        <Panel title="Reply to the thread">
          {writers.length === 0 ? (
            <Empty title="Nobody on this load uses email">Add a contact with the email channel to the load metadata.</Empty>
          ) : (
            <>
              <fieldset className="segmented segmented--wide">
                <legend className="field-label">Write as</legend>
                {writers.map((c) => (
                  <label key={c.id} className={clsx("segment", writer?.id === c.id && "segment--on")}>
                    <input type="radio" name="email-writer" value={c.id} checked={writer?.id === c.id} onChange={() => setWriterId(c.id)} />
                    {roleLabel(c.role)} <span className="mono segment-sub">{c.id}</span>
                  </label>
                ))}
              </fieldset>
              <Composer
                multiline="email"
                label={`Email body as ${writer ? `${roleLabel(writer.role)} ${writer.id}` : "a contact"}`}
                placeholder={writer?.role === "dispatcher" ? "e.g. Driver says ETA 9:40 at the shipper." : "e.g. Thanks, keep me posted."}
                sendLabel="Send email"
                disabled={!writer || !state}
                onSend={(text) => (writer ? send(writer, text) : Promise.resolve(false))}
                onAttach={(file, text) => (writer ? attach(writer, file, text) : Promise.resolve(null))}
              />
              <p className="hint">
                Inbound email carries only the new text, as the integration layer delivers it. An
                attached BOL, POD or receipt goes to the document pipeline on the next tick.
              </p>
            </>
          )}
        </Panel>
      </div>
    </>
  );
}
