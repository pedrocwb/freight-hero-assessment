import clsx from "clsx";
import { useId, useRef, useState, type KeyboardEvent } from "react";

/** What the person attached and how its upload went. */
type Upload = { state: "uploading" | "done" | "failed"; filename: string; message: string };

/** "pod_clean.pdf, 182 KB". */
function fileLabel(file: Pick<File, "name" | "size">): string {
  const kb = file.size / 1024;
  const size = kb < 1 ? `${file.size} B` : kb < 1024 ? `${Math.round(kb)} KB` : `${(kb / 1024).toFixed(1)} MB`;
  return `${file.name}, ${size}`;
}

/**
 * The message box of a role. Enter sends on chat-like channels (SMS, Slack) and
 * Shift+Enter adds a line; on email, Ctrl or Cmd+Enter sends.
 *
 * With `onAttach`, a file can go with the message (a POD photo, a BOL): it is uploaded
 * with the text as one inbound message (`POST /api/loads/{id}/attachments`), and the
 * box shows the upload until the API answers.
 */
export function Composer(props: {
  label: string;
  placeholder: string;
  sendLabel: string;
  onSend: (text: string) => Promise<boolean>;
  /** Resolves to a line about the upload (shown under the box), or null when it failed. */
  onAttach?: (file: File, text: string) => Promise<string | null>;
  accept?: string;
  disabled?: boolean;
  disabledReason?: string;
  multiline?: "chat" | "email";
  suggestions?: readonly string[];
}) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sending, setSending] = useState(false);
  const [upload, setUpload] = useState<Upload | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const id = useId();
  const mode = props.multiline ?? "chat";

  function clearFile() {
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
  }

  async function submit() {
    const value = text.trim();
    if ((!value && !file) || sending || props.disabled) return;
    setSending(true);
    if (file && props.onAttach) {
      const name = file.name;
      setUpload({ state: "uploading", filename: name, message: `Uploading ${fileLabel(file)}…` });
      const result = await props.onAttach(file, value);
      setUpload(
        result === null
          ? { state: "failed", filename: name, message: `${name} was not uploaded.` }
          : { state: "done", filename: name, message: result },
      );
      if (result !== null) {
        setText("");
        clearFile();
      }
    } else {
      const ok = await props.onSend(value);
      if (ok) setText("");
    }
    setSending(false);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter") return;
    const sendKey = mode === "chat" ? !event.shiftKey : event.metaKey || event.ctrlKey;
    if (sendKey) {
      event.preventDefault();
      void submit();
    }
  }

  const canSend = Boolean(text.trim() || file);

  return (
    <form
      className={clsx("composer", `composer--${mode}`)}
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
      {file ? (
        <p className="file-chip">
          <span className="file-chip-name">{fileLabel(file)}</span>
          <button type="button" className="file-chip-remove" onClick={clearFile} aria-label={`Remove ${file.name}`} disabled={sending}>
            ×
          </button>
        </p>
      ) : null}
      <div className="composer-bar">
        <span className="composer-hint" id={`${id}-hint`}>
          {mode === "chat" ? "Enter sends, Shift+Enter adds a line" : "Ctrl or Cmd+Enter sends"}
        </span>
        {props.onAttach ? (
          <label className={clsx("btn btn--quiet composer-attach", (props.disabled || sending) && "composer-attach--off")}>
            <input
              ref={fileInput}
              type="file"
              className="visually-hidden"
              accept={props.accept ?? ".pdf,.png,.jpg,.jpeg,.heic,image/*,application/pdf"}
              disabled={props.disabled || sending}
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setUpload(null);
              }}
            />
            Attach a file
          </label>
        ) : null}
        <button type="submit" className="btn btn--primary" disabled={props.disabled || sending || !canSend}>
          {sending ? (file ? "Uploading…" : "Sending…") : file ? `${props.sendLabel} with file` : props.sendLabel}
        </button>
      </div>
      {upload ? (
        <p className={clsx("upload-state", `upload-state--${upload.state}`)} role={upload.state === "failed" ? "alert" : "status"}>
          {upload.message}
        </p>
      ) : null}
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
