import clsx from "clsx";
import { useState } from "react";

import { api, inboundMessage, modelReady, type ConversationChannel, type DelayRisk, type Load } from "../api";
import { contactFor, roleLabel } from "../lib/load";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";
import { Composer } from "./Composer";
import { Panel } from "./ui";

const RISKS: DelayRisk[] = ["low", "medium", "high"];
const CONVERSATION: readonly string[] = ["sms", "email", "slack"];
// Stockton, CA: the pickup of the assignment's load. The API only needs plausible numbers.
const POSITION = { lat: 37.9577, lon: -121.2908 };
// Messages from the assignment's story and the scenarios, to fill in with one click.
const DRIVER_SAMPLES = ["What's the PO number?", "ok", "ETA 9:40", "give me 5 minutes", "about 30 min out", "Left the shipper"];

/**
 * Play the load's world by hand: write as one of its contacts (with a file if you like),
 * or send a delay-risk ping. The clock moves from the rail.
 */
export function FreePlay({ load }: { load: Load }) {
  const sim = useSim();
  const toasts = useToasts();
  const [senderId, setSenderId] = useState("");
  const [channelPick, setChannelPick] = useState<ConversationChannel | "">("");

  const sender = load.contacts.find((c) => c.id === senderId) ?? contactFor(load, "driver", "sms") ?? load.contacts[0];
  const channels = (sender?.channels ?? []).filter((c): c is ConversationChannel => CONVERSATION.includes(c));
  const channel = channelPick && channels.includes(channelPick) ? channelPick : channels[0];
  const blocked = sim.playing ? "A scenario is playing: it owns the clock until it ends." : !sim.state ? "Waiting for the API" : null;
  const now = sim.state?.now;

  async function send(text: string): Promise<boolean> {
    if (!sender || !channel || !now) return false;
    const message = inboundMessage({ loadId: load.load_id, channel, senderContactId: sender.id, body: text, at: now });
    const result = await sim.perform(() => api.sendMessage(message, sim.latencySeconds));
    if (!result) return false;
    if (!result.accepted) {
      toasts.error(result.reason ?? "The message was not accepted.");
      return false;
    }
    if (result.duplicate) toasts.info("The API already had this message; it was not processed again.");
    return true;
  }

  async function attach(file: File, text: string): Promise<string | null> {
    if (!sender || !channel || !now) return null;
    const upload = { file, channel, senderContactId: sender.id, body: text, at: now };
    const result = await sim.perform(() => api.uploadAttachment(load.load_id, upload, sim.latencySeconds));
    return result ? `${file.name} sent: the next tick reads document ${result.document_id}, then the agent runs.` : null;
  }

  async function ping(risk: DelayRisk) {
    if (!now) return;
    const body = { load_id: load.load_id, ...POSITION, occurred_at: now, delay_risk: risk };
    const result = await sim.perform(() => api.sendPing(body, sim.latencySeconds));
    if (!result) return;
    if (result.duplicate) toasts.info("That ping was already received; nothing changed.");
    else if (!result.accepted) toasts.error(result.reason ?? "The ping was not accepted.");
    else toasts.info(`Ping sent: ${risk} risk.`);
  }

  return (
    <Panel title="Free play">
      <div className="stack">
        {!modelReady(sim.state) ? (
          <p className="banner banner--warn" role="status">
            No model: OPENROUTER_API_KEY is unset, so the runs these inputs cause fail.
          </p>
        ) : null}
        <div className="row">
          <label className="field">
            <span className="field-label">Write as</span>
            <select className="input" value={sender?.id ?? ""} onChange={(e) => setSenderId(e.target.value)} disabled={!load.contacts.length}>
              {load.contacts.map((c) => (
                <option key={c.id} value={c.id}>
                  {roleLabel(c.role)} {c.id}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">On</span>
            <select className="input" value={channel ?? ""} onChange={(e) => setChannelPick(e.target.value as ConversationChannel)} disabled={channels.length < 2}>
              {channels.map((c) => (
                <option key={c} value={c}>
                  {c === "sms" ? "SMS" : c === "email" ? "Email" : "Slack"}
                </option>
              ))}
            </select>
          </label>
        </div>
        <Composer
          label={`Message as the ${roleLabel(sender?.role).toLowerCase()}`}
          placeholder={channel ? `A message on ${channel === "sms" ? "SMS" : channel}` : "This contact has no message channel"}
          sendLabel="Send"
          multiline={channel === "email" ? "email" : "chat"}
          disabled={Boolean(blocked) || !channel}
          disabledReason={blocked ?? "This contact has no message channel"}
          suggestions={sender?.role === "driver" ? DRIVER_SAMPLES : undefined}
          onSend={send}
          onAttach={attach}
        />
        <fieldset className="segmented ping-buttons" disabled={Boolean(blocked)}>
          <legend className="field-label">Delay-risk ping</legend>
          {RISKS.map((risk) => (
            <button key={risk} type="button" className={clsx("segment", `segment--${risk}`)} onClick={() => void ping(risk)}>
              {risk.charAt(0).toUpperCase() + risk.slice(1)}
            </button>
          ))}
        </fieldset>
        <label className="field field--inline">
          <span className="field-label">Run after, seconds of load time</span>
          <input
            className="input input--small"
            type="number"
            min={0}
            max={3600}
            value={sim.latencySeconds}
            onChange={(e) => sim.setLatencySeconds(Math.max(0, Math.min(3600, Number(e.target.value) || 0)))}
          />
        </label>
        <p className="hint">
          {blocked ??
            "Each input is stamped with the load's time; its run follows after the latency (the scenarios use 60 s), so move the clock. Only a ping into or out of high wakes the agent."}
        </p>
      </div>
    </Panel>
  );
}
