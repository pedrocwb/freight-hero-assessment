import clsx from "clsx";
import { useEffect, useRef } from "react";

import { Composer } from "../components/Composer";
import { LedgerPanel } from "../components/Ledger";
import { Empty, InlineError, NoLoad, ScreenHead } from "../components/ui";
import { useThread } from "../hooks/useThread";
import { groupByDay } from "../lib/conversation";
import { contactFor } from "../lib/load";
import { formatTime } from "../lib/time";
import { useSim } from "../state/sim";

// Messages from the assignment's story and the eval scenarios, to fill in with one click.
const SAMPLES = ["What's the PO number?", "ok", "ETA 9:40", "give me 5 minutes", "about 30 min out", "Left the shipper"];

export function DriverPhone() {
  const sim = useSim();
  const { load, timezone } = sim;
  const driver = contactFor(load, "driver", "sms");
  const { resource, entries, send, discard } = useThread("sms");
  const thread = useRef<HTMLDivElement>(null);

  // Keep the newest text in view inside the phone, without scrolling the page.
  useEffect(() => {
    const el = thread.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries.length]);

  if (!sim.selectedLoadId) {
    return (
      <>
        <ScreenHead title="Driver phone" />
        <NoLoad what="the driver's SMS thread" />
      </>
    );
  }

  return (
    <>
      <ScreenHead
        title="Driver phone"
        lede={
          driver ? (
            <>
              You are the driver <span className="mono">{driver.id}</span>
              {load ? `, ${load.carrier_name}` : ""}. What you send reaches the agent as an SMS.
            </>
          ) : (
            "This load has no driver contact on SMS."
          )
        }
      />
      <div className="grid grid--phone">
        <div className="phone" aria-label="Driver's phone">
          <div className="phone-head">
            <span className="phone-avatar" aria-hidden="true">
              {load?.broker_name.slice(0, 1) ?? "B"}
            </span>
            <div>
              <p className="phone-name">{load?.broker_name ?? "Broker"}</p>
              <p className="phone-sub">Load {sim.selectedLoadId}, text messages</p>
            </div>
          </div>
          <div className="phone-thread" role="log" aria-label="SMS thread" aria-live="polite" ref={thread} tabIndex={0}>
            {resource.error ? <InlineError message={resource.error} onRetry={() => void resource.reload()} /> : null}
            {resource.data && entries.length === 0 ? (
              <Empty title="No texts yet">
                Send a high delay-risk ping from the Control room and the agent texts the driver
                for an ETA. Or write first below.
              </Empty>
            ) : null}
            {groupByDay(entries, timezone).map((group) => (
              <div key={group.key} className="day">
                <p className="day-label">{group.label}</p>
                {group.items.map((entry) => {
                  const own = entry.direction === "in";
                  return (
                    <div key={entry.key} className={clsx("bubble-row", own ? "bubble-row--own" : "bubble-row--them")}>
                      <div className={clsx("bubble", own ? "bubble--own" : "bubble--them", entry.pending === "failed" && "bubble--failed")}>
                        <span className="visually-hidden">{own ? "You" : "Agent"}: </span>
                        {entry.text}
                      </div>
                      <p className="bubble-meta">
                        <time dateTime={entry.at}>{formatTime(entry.at, timezone)}</time>
                        {entry.pending === "sending" ? ", sending" : null}
                        {entry.pending === "sent" ? ", delivered" : null}
                        {entry.status && !own ? `, ${entry.status}` : null}
                        {entry.pending === "failed" ? (
                          <>
                            , not sent: {entry.error}{" "}
                            <button type="button" className="linklike" onClick={() => discard(entry.key)}>
                              Remove
                            </button>
                          </>
                        ) : null}
                      </p>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
          <Composer
            className="phone-composer"
            label="Text message as the driver"
            placeholder="Text message"
            sendLabel="Send"
            disabled={!driver || !sim.state}
            disabledReason="This load has no driver on SMS"
            suggestions={SAMPLES}
            onSend={(text) => (driver ? send(driver, text) : Promise.resolve(false))}
          />
        </div>
        <div className="col-side">
          <LedgerPanel snapshot={sim.snapshot.data} compact />
          <p className="hint side-hint">
            A question or an "ok" is not an ETA: the request stays open and its follow-up still
            runs. Move the clock from the left to reach the next timer.
          </p>
        </div>
      </div>
    </>
  );
}
