import clsx from "clsx";
import { useId, useRef, useState, type KeyboardEvent } from "react";

import type { ConversationChannel, DelayRisk, Load } from "../api";
import { roleLabel } from "../lib/load";
import { formatTime } from "../lib/time";

const CONVERSATION: readonly string[] = ["sms", "email", "slack"];
const CHANNEL_LABEL: Record<ConversationChannel, string> = { sms: "SMS", email: "Email", slack: "Slack" };
const RISKS: DelayRisk[] = ["low", "medium", "high"];

export interface Voice {
  key: string;
  contactId: string;
  role: string;
  channel: ConversationChannel;
  label: string;
}

/** Every contact of the load on each of its message channels: "Driver, SMS". */
export function voices(load: Pick<Load, "contacts">): Voice[] {
  return load.contacts.flatMap((c) =>
    c.channels
      .filter((ch): ch is ConversationChannel => CONVERSATION.includes(ch))
      .map((channel) => ({ key: `${c.id}:${channel}`, contactId: c.id, role: c.role, channel, label: `${roleLabel(c.role)}, ${CHANNEL_LABEL[channel]}` })),
  );
}

/**
 * Write as one of the load's contacts and wait for the agent: "Send and wait" sends, lets
 * the latency pass and runs the queue. Quieter beside it: a location ping, and the jump
 * to the next timer.
 */
export function ComposerBar(props: {
  load: Load;
  disabled: string | null;
  waiting: boolean;
  nextTimer: string | null;
  timezone: string;
  onSend: (voice: Voice, text: string, file: File | null) => Promise<boolean>;
  onPing: (risk: DelayRisk) => void;
  onSkip: () => void;
}) {
  const options = voices(props.load);
  const [voiceKey, setVoiceKey] = useState(options[0]?.key ?? "");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const id = useId();
  const voice = options.find((v) => v.key === voiceKey) ?? options[0];
  const blocked = Boolean(props.disabled) || props.waiting;
  const canSend = Boolean(voice) && !blocked && Boolean(text.trim() || file);

  async function submit() {
    if (!canSend || !voice) return;
    // The message moves into the chat at once; it comes back here if it was not sent.
    const sentText = text.trim();
    const sentFile = file;
    setText("");
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
    if (!(await props.onSend(voice, sentText, sentFile))) {
      setText(sentText);
      setFile(sentFile);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && voice?.channel !== "email") {
      event.preventDefault();
      void submit();
    } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <div className="composer">
      <form
        className="composer-main"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="composer-voice">
          <span className="composer-voice-label">Write as</span>
          <select className="select" value={voice?.key ?? ""} onChange={(e) => setVoiceKey(e.target.value)} disabled={!options.length}>
            {options.map((v) => (
              <option key={v.key} value={v.key}>
                {v.label}
              </option>
            ))}
          </select>
        </label>
        <div className="composer-box">
          <label className="visually-hidden" htmlFor={id}>
            Message as the {roleLabel(voice?.role ?? null).toLowerCase()}
          </label>
          <textarea
            id={id}
            className="composer-input"
            rows={1}
            value={text}
            placeholder={props.disabled ?? (voice ? `Message as the ${roleLabel(voice.role).toLowerCase()}` : "This load has no contact to write as")}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={Boolean(props.disabled)}
          />
          {file ? (
            <p className="composer-file">
              <span>{file.name}</span>
              <button type="button" className="link-button" onClick={() => setFile(null)} aria-label={`Remove ${file.name}`}>
                Remove
              </button>
            </p>
          ) : null}
        </div>
        <label className={clsx("icon-button", blocked && "icon-button--off")} title="Attach a file (a POD photo, a BOL)">
          <input
            ref={fileInput}
            type="file"
            className="visually-hidden"
            accept=".pdf,.png,.jpg,.jpeg,.heic,.txt,image/*,application/pdf,text/plain"
            disabled={blocked}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M13.5 6.5 7.8 12.2a1.6 1.6 0 0 0 2.3 2.3l6-6a3.2 3.2 0 0 0-4.6-4.6l-6.2 6.2a4.8 4.8 0 0 0 6.8 6.8l5-5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
          <span className="visually-hidden">Attach a file</span>
        </label>
        <button type="submit" className="button button--primary" disabled={!canSend}>
          {props.waiting ? "Waiting…" : "Send and wait"}
        </button>
      </form>
      <div className="composer-more">
        <span className="composer-more-label">Location ping</span>
        <span className="segmented" role="group" aria-label="Send a location ping with this delay risk">
          {RISKS.map((risk) => (
            <button key={risk} type="button" className={clsx("segment", risk === "high" && "segment--high")} disabled={blocked} onClick={() => props.onPing(risk)}>
              {risk.charAt(0).toUpperCase() + risk.slice(1)}
            </button>
          ))}
        </span>
        <button type="button" className="button button--quiet" disabled={blocked || !props.nextTimer} onClick={props.onSkip} title={props.nextTimer ? "Move the clock to the next timer and run it" : "No timer is pending"}>
          {props.nextTimer ? `Skip to next timer, ${formatTime(props.nextTimer, props.timezone)}` : "No timer pending"}
        </button>
      </div>
    </div>
  );
}
