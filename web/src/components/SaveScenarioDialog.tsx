import clsx from "clsx";
import { useEffect, useState } from "react";

import { api, ApiError, type LoadSnapshot, type RecordedStep, type SavedScenario } from "../api";
import { href } from "../lib/route";
import { draftSteps, FORBID_KEYS, FORBIDS, ID_PATTERN, suggestId, suggestTags, toDraft, type DraftStep, type ForbidKey } from "../lib/scenarioDraft";
import { Modal } from "./Modal";

/**
 * "Save as scenario": the conversation's recorded steps, each with the expectations drafted
 * from what happened, to uncheck, plus forbid lines and a note; saved as an eval scenario
 * file (`POST /api/scenarios`), which the Scenarios screen and the evals then pick up.
 */
export function SaveScenarioDialog(props: {
  open: boolean;
  onClose: () => void;
  snapshot: LoadSnapshot;
  recording: readonly RecordedStep[];
  timezone: string;
  now: string | undefined;
}) {
  const { snapshot, recording } = props;
  const loadId = snapshot.load.load_id;
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [suite, setSuite] = useState("core");
  const [tags, setTags] = useState("");
  const [steps, setSteps] = useState<DraftStep[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<SavedScenario | null>(null);

  // A fresh draft each time the dialog opens, from the conversation as it is now.
  useEffect(() => {
    if (!props.open) return;
    setId(suggestId(loadId, props.now ?? new Date().toISOString(), props.timezone));
    setTitle(`Load ${loadId}: ${recording.length} step${recording.length === 1 ? "" : "s"} recorded on the platform`);
    setSuite("core");
    setTags(suggestTags(snapshot.runs, recording).join(", "));
    setSteps(draftSteps(recording, snapshot.outbox, snapshot.load, props.timezone));
    setError(null);
    setSaved(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.open]);

  const idProblem = id && !ID_PATTERN.test(id) ? "Lowercase letters, digits and underscores, starting with a letter." : null;

  function update(index: number, change: (step: DraftStep) => DraftStep) {
    setSteps((list) => list.map((s) => (s.index === index ? change(s) : s)));
  }

  async function save() {
    if (saved) {
      props.onClose();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      setSaved(await api.saveScenario(toDraft({ loadId, id, title, suite, tags, description: "", steps })));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={props.open}
      wide
      title="Save as scenario"
      submitLabel={saved ? "Done" : saving ? "Saving…" : "Save scenario"}
      submitDisabled={saving || !id || Boolean(idProblem) || !title.trim() || !steps.length}
      onClose={props.onClose}
      onSubmit={() => void save()}
    >
      {saved ? (
        <div className="saved" role="status">
          <p>
            Saved as <code>evals/{saved.path}</code>.
          </p>
          <p className="why-sub">
            It is in the <a href={href("scenarios", { id: saved.id })}>Scenarios</a> list, in <code>agent eval list</code>, and in the next LangSmith experiment of
            the {saved.suite} suite.
          </p>
        </div>
      ) : (
        <>
          <div className="form-grid">
            <label className="field">
              <span className="field-label">Id</span>
              <input className="input mono" value={id} onChange={(e) => setId(e.target.value.trim())} spellCheck={false} aria-invalid={idProblem ? true : undefined} />
              {idProblem ? <span className="field-error">{idProblem}</span> : null}
            </label>
            <label className="field">
              <span className="field-label">Suite</span>
              <input className="input mono" value={suite} onChange={(e) => setSuite(e.target.value.trim())} spellCheck={false} />
            </label>
            <label className="field field--wide">
              <span className="field-label">Title</span>
              <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label className="field field--wide">
              <span className="field-label">Tags</span>
              <input className="input mono" value={tags} onChange={(e) => setTags(e.target.value)} spellCheck={false} />
            </label>
          </div>
          {!steps.length ? <p className="why-sub">Nothing was recorded for this load yet: send a message, a ping or skip to a timer first.</p> : null}
          <ol className="draft-steps">
            {steps.map((step, n) => (
              <li key={step.index} className="draft-step">
                <p className="draft-step-head">
                  <span className="draft-step-time">{step.at}</span>
                  <span>
                    Step {n + 1}. {step.summary}
                  </span>
                </p>
                <label className="field">
                  <span className="visually-hidden">Note of step {n + 1}</span>
                  <input className="input" value={step.note} onChange={(e) => update(step.index, (s) => ({ ...s, note: e.target.value }))} />
                </label>
                <fieldset className="checks">
                  <legend className="field-label">Expect</legend>
                  {step.expectations.map((e) => (
                    <label key={e.id} className={clsx("check", !e.on && "check--off")}>
                      <input
                        type="checkbox"
                        checked={e.on}
                        onChange={() => update(step.index, (s) => ({ ...s, expectations: s.expectations.map((x) => (x.id === e.id ? { ...x, on: !x.on } : x)) }))}
                      />
                      {e.label}
                    </label>
                  ))}
                </fieldset>
                <fieldset className="checks">
                  <legend className="field-label">Forbid</legend>
                  {FORBID_KEYS.map((key: ForbidKey) => (
                    <label key={key} className={clsx("check", !step.forbid.includes(key) && "check--off")}>
                      <input
                        type="checkbox"
                        checked={step.forbid.includes(key)}
                        onChange={() =>
                          update(step.index, (s) => ({ ...s, forbid: s.forbid.includes(key) ? s.forbid.filter((k) => k !== key) : [...s.forbid, key] }))
                        }
                      />
                      {FORBIDS[key].label}
                    </label>
                  ))}
                </fieldset>
              </li>
            ))}
          </ol>
          {error ? (
            <p className="field-error" role="alert">
              {error}
            </p>
          ) : null}
        </>
      )}
    </Modal>
  );
}
