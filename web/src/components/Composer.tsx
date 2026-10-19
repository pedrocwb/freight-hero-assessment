import clsx from "clsx";
import { useId, useState, type KeyboardEvent } from "react";

/**
 * The message box of a role. Enter sends on chat-like channels (SMS, Slack) and
 * Shift+Enter adds a line; on email, Ctrl or Cmd+Enter sends.
 */
export function Composer(props: {
  label: string;
  placeholder: string;
  sendLabel: string;
  onSend: (text: string) => Promise<boolean>;
  disabled?: boolean;
  disabledReason?: string;
  multiline?: "chat" | "email";
  suggestions?: readonly string[];
  className?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const id = useId();
  const mode = props.multiline ?? "chat";

  async function submit() {
    const value = text.trim();
    if (!value || sending || props.disabled) return;
    setSending(true);
    const ok = await props.onSend(value);
    setSending(false);
    if (ok) setText("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter") return;
    const sendKey = mode === "chat" ? !event.shiftKey : event.metaKey || event.ctrlKey;
    if (sendKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form
      className={clsx("composer", `composer--${mode}`, props.className)}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label className="visually-hidden" htmlFor={id}>
        {props.label}
      </label>
      <textarea
        id={id}
        className="input composer-input"
        rows={mode === "chat" ? 2 : 5}
        value={text}
        placeholder={props.disabled ? props.disabledReason ?? props.placeholder : props.placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        disabled={props.disabled}
        aria-describedby={`${id}-hint`}
      />
      <div className="composer-bar">
        <span className="composer-hint" id={`${id}-hint`}>
          {mode === "chat" ? "Enter sends, Shift+Enter adds a line" : "Ctrl or Cmd+Enter sends"}
        </span>
        <button type="submit" className="btn btn--primary" disabled={props.disabled || sending || !text.trim()}>
          {sending ? "Sending…" : props.sendLabel}
        </button>
      </div>
      {props.suggestions?.length ? (
        <div className="suggestions" role="group" aria-label="Fill in a sample message">
          {props.suggestions.map((s) => (
            <button key={s} type="button" className="suggestion" onClick={() => setText(s)} disabled={props.disabled}>
              {s}
            </button>
          ))}
        </div>
      ) : null}
    </form>
  );
}
