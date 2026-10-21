import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type Fixture, type LoadMetadata } from "../api";
import { useResource } from "../hooks/useResource";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";
import { Modal } from "./Modal";
import { InlineError } from "./ui";

/** A load from a fixture's metadata (assignment JSON), editable before it is created. */
export function NewLoadDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const sim = useSim();
  const toasts = useToasts();
  const fixtures = useResource(open ? "fixtures" : null, () => api.fixtures(), []);
  const [chosen, setChosen] = useState<string>("");
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);

  const list: Fixture[] = useMemo(() => fixtures.data ?? [], [fixtures.data]);
  const pick = useCallback(
    (name: string) => {
      setChosen(name);
      const fixture = list.find((f) => f.name === name);
      setText(fixture ? JSON.stringify(fixture.metadata, null, 2) : "");
      setParseError(null);
    },
    [list],
  );
  useEffect(() => {
    if (open && !chosen && list[0]) pick(list[0].name);
  }, [open, chosen, list, pick]);

  function close() {
    setChosen("");
    onClose();
  }

  async function create() {
    let metadata: LoadMetadata;
    try {
      const parsed: unknown = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("The metadata must be a JSON object.");
      metadata = parsed as LoadMetadata;
    } catch (error) {
      setParseError(error instanceof Error ? error.message : String(error));
      return;
    }
    const summary = await sim.perform(() => api.createLoad(metadata));
    if (summary) {
      sim.addLoad(summary);
      toasts.info(`Load ${summary.load_id} created for ${summary.broker_name}.`);
      close();
    }
  }

  return (
    <Modal
      open={open}
      wide
      title="New load"
      submitLabel="Create load"
      submitDisabled={!text.trim() || sim.busy || sim.playing}
      onClose={close}
      onSubmit={() => void create()}
    >
      {fixtures.error ? <InlineError message={fixtures.error} onRetry={() => void fixtures.reload()} /> : null}
      <label className="field">
        <span className="field-label">Fixture</span>
        <select className="input" value={chosen} onChange={(e) => pick(e.target.value)} disabled={!list.length}>
          {!list.length ? <option value="">{fixtures.loading ? "Loading fixtures…" : "No fixture found"}</option> : null}
          {list.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="field-label">Load metadata (assignment JSON, editable; change load_id to add a second copy)</span>
        <textarea
          className="input mono json-editor"
          value={text}
          rows={16}
          spellCheck={false}
          onChange={(e) => {
            setText(e.target.value);
            setParseError(null);
          }}
          aria-invalid={parseError ? true : undefined}
          aria-describedby={parseError ? "json-error" : undefined}
        />
      </label>
      {parseError ? (
        <p className="field-error" id="json-error" role="alert">
          {parseError}
        </p>
      ) : null}
    </Modal>
  );
}
