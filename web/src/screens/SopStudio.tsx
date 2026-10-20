import clsx from "clsx";
import { useCallback, useMemo, useRef, useState, type DragEvent } from "react";

import {
  api,
  draftInput,
  STAGES,
  type BrokerView,
  type DraftSection,
  type EvalMode,
  type ObligationDef,
  type ProposedIntent,
  type SopDraftView,
  type SopPublishResult,
  type Stage,
} from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag } from "../components/ui";
import { useBrokerChoice, useBrokerList } from "../hooks/useBrokers";
import { useResource } from "../hooks/useResource";
import { removeAt, replaceAt, sameJson } from "../lib/broker";
import { invalidate } from "../lib/bus";
import type { EvalSummary } from "../lib/evals";
import { humanize, stageLabel } from "../lib/load";
import { decideIntent, draftProblems, evidenceLines, fileNames, requirementLabel, sourceLabel, undecided } from "../lib/sopDraft";
import { formatStamp } from "../lib/time";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";
import { DEFAULT_COST_CAP_USD, EVAL_MODES, EvalResults } from "./Evals";

const SOP_ACCEPT = ".pdf,.md,.markdown,application/pdf,text/markdown";
const NAMES_KEY = "freight-sim.sop-files";

/**
 * The file names of the SOPs uploaded from this browser, by document id. The draft view
 * names its sources by id only, so the page remembers what it sent.
 */
function useUploadedNames(): [Record<string, string>, (documentId: string, filename: string) => void] {
  const [names, setNames] = useState<Record<string, string>>(() => {
    try {
      const parsed: unknown = JSON.parse(window.localStorage.getItem(NAMES_KEY) ?? "{}");
      return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
    } catch {
      return {};
    }
  });
  const remember = useCallback((documentId: string, filename: string) => {
    setNames((current) => {
      const next = { ...current, [documentId]: filename };
      try {
        window.localStorage.setItem(NAMES_KEY, JSON.stringify(next));
      } catch {
        // Storage may be refused; the names then last for the visit.
      }
      return next;
    });
  }, []);
  return [names, remember];
}

function isSopFile(file: File): boolean {
  return /\.(pdf|md|markdown)$/i.test(file.name) || file.type === "application/pdf" || file.type === "text/markdown";
}

export function SopStudio() {
  const brokers = useBrokerList();
  const [chosen, choose] = useBrokerChoice(brokers.data);

  return (
    <>
      <ScreenHead
        title="SOP studio"
        lede="Onboard a broker's procedure without code: upload its SOP, review the sections and the intents the compiler proposes, publish a version (as a canary if you like) and run the broker's evals."
        actions={
          <label className="load-picker">
            <span className="load-picker-label">Broker</span>
            <select className="input" value={chosen ?? ""} onChange={(e) => choose(e.target.value)} disabled={!brokers.data?.length}>
              {!brokers.data?.length ? <option value="">{brokers.loading ? "Loading…" : "No broker"}</option> : null}
              {(brokers.data ?? []).map((b) => (
                <option key={b.broker_id} value={b.broker_id}>
                  {b.name} ({b.broker_id})
                </option>
              ))}
            </select>
          </label>
        }
      />
      {brokers.error ? <InlineError message={brokers.error} onRetry={() => void brokers.reload()} /> : null}
      {chosen ? (
        <Studio key={chosen} brokerId={chosen} />
      ) : (
        <Panel title="No broker">
          <Empty title="Create a broker first">On the Brokers screen, New broker makes a copy of the standard one.</Empty>
        </Panel>
      )}
    </>
  );
}

function Studio({ brokerId }: { brokerId: string }) {
  const toasts = useToasts();
  const broker = useResource(`broker:${brokerId}`, () => api.getBroker(brokerId), ["brokers"]);
  const draft = useResource(`draft:${brokerId}`, () => api.sopDraft(brokerId), ["knowledge"]);
  const [edit, setEdit] = useState<SopDraftView | null>(null);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const current = edit ?? draft.data ?? null;
  const dirty = edit !== null && draft.data !== null && draft.data !== undefined && !sameJson(draftInput(edit), draftInput(draft.data));

  const [uploaded, remember] = useUploadedNames();

  const brokerIntents = useMemo(() => (broker.data?.intents ?? []).map((i) => i.intent_id), [broker.data]);
  const documentTypes = useMemo(() => (broker.data?.document_types ?? []).map((t) => t.type_id), [broker.data]);
  const obligationKinds = useMemo(() => (broker.data?.obligations ?? []).map((o) => o.kind), [broker.data]);
  const problems = current && broker.data ? draftProblems(current, brokerIntents, documentTypes, obligationKinds) : [];
  const update = (patch: Partial<SopDraftView>) => current && setEdit({ ...current, ...patch });

  async function save(): Promise<boolean> {
    if (!edit) return true;
    setSaving(true);
    try {
      await api.saveSopDraft(brokerId, draftInput(edit));
      setEdit(null);
      invalidate("knowledge");
      return true;
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      setSaving(false);
    }
  }

  const names = fileNames(current?.documents, uploaded);
  const sections = current?.sections ?? [];
  const proposedCount = sections.filter((s) => s.proposed).length;
  const isOpen = (s: DraftSection) => open[s.section_id] ?? s.proposed;

  return (
    <>
      <UploadPanel
        brokerId={brokerId}
        hasDraft={Boolean(draft.data)}
        onFile={remember}
        onUploaded={() => {
          setEdit(null);
          setOpen({});
          invalidate("knowledge");
        }}
      />
      <div className="grid grid--inspector grid--studio">
        <section className="col-main" aria-labelledby="draft-title">
          <Panel
            id="draft"
            title={
              <>
                Draft
                {current ? (
                  <span className="panel-title-sub">
                    {sections.length} section{sections.length === 1 ? "" : "s"}, {proposedCount} to review
                  </span>
                ) : null}
              </>
            }
            actions={
              dirty ? (
                <>
                  <span className="muted">Unsaved changes</span>
                  <button type="button" className="btn btn--small" onClick={() => setEdit(null)} disabled={saving}>
                    Discard
                  </button>
                  <button type="button" className="btn btn--small btn--primary" onClick={() => void save()} disabled={saving}>
                    {saving ? "Saving…" : "Save draft"}
                  </button>
                </>
              ) : null
            }
          >
            {draft.error ? <InlineError message={draft.error} onRetry={() => void draft.reload()} /> : null}
            {draft.data === null ? (
              <Empty title={`No draft for ${broker.data?.name ?? brokerId} yet`}>
                Upload the broker's SOP above. The compiler splits it into sections, proposes which
                intents and stages each one applies to, and proposes new intents and document
                requirements for you to accept or reject.
              </Empty>
            ) : null}
            {current ? (
              <div className="stack">
                <DraftFacts draft={current} names={names} />
                <Problems draft={current} dirty={dirty} problems={problems} />
              </div>
            ) : null}
          </Panel>
          {sections.map((section, index) => (
            <DraftSectionCard
              key={`${section.section_id}-${index}`}
              section={section}
              names={names}
              broker={broker.data}
              proposed={current?.proposed_intents ?? []}
              open={isOpen(section)}
              onToggle={() => setOpen((o) => ({ ...o, [section.section_id]: !isOpen(section) }))}
              onChange={(patch) => update({ sections: replaceAt(sections, index, patch) })}
              onRemove={() => update({ sections: removeAt(sections, index) })}
            />
          ))}
        </section>
        <div className="col-side">
          {current ? (
            <>
              <ProposedIntentsPanel draft={current} onDecide={(id, accepted) => setEdit(decideIntent(current, id, accepted))} />
              <ProposedRequirementsPanel draft={current} onRemove={(index) => update({ proposed_requirements: removeAt(current.proposed_requirements, index) })} />
              <ProposedObligationsPanel
                obligations={current.proposed_obligations ?? []}
                existing={obligationKinds}
                onRemove={(index) => update({ proposed_obligations: removeAt(current.proposed_obligations ?? [], index) })}
              />
              <PublishPanel brokerId={brokerId} draft={current} problems={problems} dirty={dirty} save={save} onPublished={() => setEdit(null)} />
            </>
          ) : null}
          <VersionsPanel brokerId={brokerId} broker={broker.data} />
        </div>
      </div>
      <BrokerEvalsPanel brokerId={brokerId} broker={broker.data} />
    </>
  );
}

// --- upload -------------------------------------------------------------------------------

type UploadState = { file: File; state: "waiting" | "uploading" | "done" | "failed"; message: string };

function UploadPanel(props: {
  brokerId: string;
  hasDraft: boolean;
  onFile: (documentId: string, filename: string) => void;
  onUploaded: () => void;
}) {
  const toasts = useToasts();
  const [uploads, setUploads] = useState<UploadState[]>([]);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  function choose(files: FileList | null) {
    const list = [...(files ?? [])];
    const wrong = list.filter((f) => !isSopFile(f));
    if (wrong.length) toasts.error(`Only PDF and Markdown SOPs can be read: ${wrong.map((f) => f.name).join(", ")}`);
    const ok = list.filter(isSopFile);
    if (ok.length) setUploads(ok.map((file) => ({ file, state: "waiting", message: "Ready to upload" })));
  }

  async function upload() {
    setBusy(true);
    let any = false;
    for (let i = 0; i < uploads.length; i += 1) {
      const item = uploads[i]!;
      if (item.state === "done") continue;
      const set = (patch: Partial<UploadState>) => setUploads((list) => list.map((u, j) => (j === i ? { ...u, ...patch } : u)));
      set({ state: "uploading", message: "Uploading and compiling…" });
      try {
        const result = await api.uploadSop(props.brokerId, item.file);
        props.onFile(result.document_id, item.file.name);
        const failed = result.status === "failed";
        set({
          state: failed ? "failed" : "done",
          message: `${result.pages} page${result.pages === 1 ? "" : "s"} read, ${result.status}${result.status === "stored" ? " (not compiled: compiling needs LLM_MODE=live)" : ""}`,
        });
        any = true;
      } catch (error) {
        set({ state: "failed", message: error instanceof Error ? error.message : String(error) });
      }
    }
    setBusy(false);
    if (any) {
      if (input.current) input.current.value = "";
      props.onUploaded();
    }
  }

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setOver(false);
    choose(event.dataTransfer.files);
  };
  const pending = uploads.filter((u) => u.state === "waiting" || u.state === "failed").length;

  return (
    <Panel title="Upload the SOP">
      <div className="upload">
        <label
          className={clsx("dropzone", over && "dropzone--over")}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={onDrop}
        >
          <input ref={input} type="file" className="visually-hidden" accept={SOP_ACCEPT} multiple onChange={(e) => choose(e.target.files)} disabled={busy} />
          <span className="dropzone-title">Drop PDF or Markdown files here, or choose them</span>
          <span className="hint">A scanned page is read by the vision model; compiling needs LLM_MODE=live (about $0.05 a document).</span>
        </label>
        <div className="upload-side">
          {uploads.length ? (
            <ul className="upload-list">
              {uploads.map((u) => (
                <li key={u.file.name} className={clsx("upload-item", `upload-item--${u.state}`)}>
                  <span className="mono">{u.file.name}</span>
                  <span className="cell-sub">{u.message}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No file chosen.</p>
          )}
          <button type="button" className="btn btn--primary" disabled={busy || pending === 0} onClick={() => void upload()}>
            {busy ? "Compiling…" : pending > 1 ? `Upload and compile ${pending} files` : "Upload and compile"}
          </button>
          {props.hasDraft ? <p className="hint">Compiling replaces the draft below, and unsaved edits are lost. Published versions stay.</p> : null}
        </div>
      </div>
    </Panel>
  );
}

// --- the draft ------------------------------------------------------------------------------

function DraftFacts({ draft, names }: { draft: SopDraftView; names: Record<string, string> }) {
  const { timezone, state } = useSim();
  const files = draft.document_ids.map((id) => names[id] ?? id);
  return (
    <dl className="facts">
      <div className="facts-wide">
        <dt>Compiled from</dt>
        <dd>
          {files.length ? (
            files.map((f, i) => (
              <span key={f}>
                {i > 0 ? ", " : ""}
                <span className="mono">{f}</span>
              </span>
            ))
          ) : (
            <span className="muted">no document</span>
          )}
          {draft.documents?.length ? (
            <span className="muted">
              {" "}
              ({draft.documents.reduce((n, d) => n + d.pages, 0)} pages)
            </span>
          ) : null}
        </dd>
      </div>
      <div>
        <dt>Last change</dt>
        <dd>{formatStamp(draft.updated_at, timezone, state?.now)}</dd>
      </div>
      <div>
        <dt>Proposed intents</dt>
        <dd>
          {draft.proposed_intents.length} ({undecided(draft)} waiting)
        </dd>
      </div>
      <div>
        <dt>Proposed requirements and obligations</dt>
        <dd>
          {draft.proposed_requirements.length} and {(draft.proposed_obligations ?? []).length}
        </dd>
      </div>
    </dl>
  );
}

function Problems({ draft, dirty, problems }: { draft: SopDraftView; dirty: boolean; problems: string[] }) {
  // The API's lint is for the saved draft; while editing, the page's own check stands in.
  const list = dirty ? problems : draft.lint.length ? draft.lint : problems;
  if (!list.length) {
    return <p className="lint lint--ok">Lint passes: every section applies to known intents and every accepted intent has a section.</p>;
  }
  return (
    <div className="lint" role="status">
      <p className="strong">
        {list.length} problem{list.length === 1 ? "" : "s"} to fix before publishing{dirty ? " (checked on the page; saving runs the API's lint)" : ""}
      </p>
      <ul className="problems">
        {list.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
    </div>
  );
}

function DraftSectionCard(props: {
  section: DraftSection;
  names: Record<string, string>;
  broker: BrokerView | undefined;
  proposed: ProposedIntent[];
  open: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<DraftSection>) => void;
  onRemove: () => void;
}) {
  const { section } = props;
  const headingId = `draft-${section.section_id}`;
  // Intents the section may apply to: the broker's, the proposals not rejected, and any it names already.
  const options = useMemo(() => {
    const list: { id: string; label: string; proposed: boolean }[] = [];
    for (const i of props.broker?.intents ?? []) list.push({ id: i.intent_id, label: i.title || humanize(i.intent_id), proposed: false });
    for (const p of props.proposed) {
      if (p.accepted === false || list.some((o) => o.id === p.intent_id)) continue;
      list.push({ id: p.intent_id, label: p.title || humanize(p.intent_id), proposed: true });
    }
    for (const id of section.intents) if (!list.some((o) => o.id === id)) list.push({ id, label: humanize(id), proposed: false });
    return list;
  }, [props.broker, props.proposed, section.intents]);
  const toggleIntent = (id: string, on: boolean) =>
    props.onChange({ intents: on ? [...section.intents, id] : section.intents.filter((i) => i !== id) });
  const toggleStage = (stage: Stage, on: boolean) =>
    props.onChange({ stages: on ? STAGES.filter((s) => s === stage || section.stages.includes(s)) : section.stages.filter((s) => s !== stage) });
  const title = section.title || (section.mode === "extend" ? `Adds to ${humanize(section.section_id)}` : humanize(section.section_id));

  return (
    <article className={clsx("sop draft-section", section.proposed && "draft-section--proposed")} aria-labelledby={headingId}>
      <header className="sop-head">
        <h3 className="sop-title" id={headingId}>
          {title}
          {section.proposed ? <Tag tone="proposed">Proposed</Tag> : <Tag tone="sop">Reviewed</Tag>}
          <Tag tone="muted">{section.mode === "extend" ? "Extends the standard section" : "Replaces the standard section"}</Tag>
        </h3>
        <span className="mono sop-id">{section.section_id}</span>
      </header>
      {section.description ? <p className="sop-desc">{section.description}</p> : null}
      <dl className="sop-meta">
        <div>
          <dt>Applies to intents</dt>
          <dd>{section.intents.length ? section.intents.join(", ") : "every intent"}</dd>
        </div>
        <div>
          <dt>Stages</dt>
          <dd>{section.stages.length ? section.stages.map(stageLabel).join(", ") : "every stage"}</dd>
        </div>
        <div className="sop-meta-wide">
          <dt>Source</dt>
          <dd>{sourceLabel(section.source, props.names)}</dd>
        </div>
      </dl>
      <div className="draft-actions">
        <button type="button" className="btn btn--small" aria-expanded={props.open} onClick={props.onToggle}>
          {props.open ? "Close" : "Edit"}
        </button>
        {section.proposed ? (
          <button type="button" className="btn btn--small" onClick={() => props.onChange({ proposed: false })}>
            Mark reviewed
          </button>
        ) : null}
        <button type="button" className="btn btn--small btn--quiet" onClick={props.onRemove}>
          Remove section
        </button>
      </div>
      {props.open ? (
        <div className="draft-form">
          <div className="form-grid">
            <label className="field">
              <span className="field-label">Title{section.mode === "extend" ? " (empty keeps the standard one)" : ""}</span>
              <input className="input" value={section.title} onChange={(e) => props.onChange({ title: e.target.value })} />
            </label>
            <fieldset className="segmented">
              <legend className="field-label">Mode</legend>
              {(["replace", "extend"] as const).map((m) => (
                <label key={m} className={clsx("segment", section.mode === m && "segment--on")}>
                  <input type="radio" name={`mode-${section.section_id}`} value={m} checked={section.mode === m} onChange={() => props.onChange({ mode: m })} />
                  {m === "replace" ? "Replace" : "Extend"}
                </label>
              ))}
            </fieldset>
            <label className="field form-grid-wide">
              <span className="field-label">Summary, as the agent's catalog shows it</span>
              <input className="input" value={section.description} onChange={(e) => props.onChange({ description: e.target.value })} />
            </label>
          </div>
          <fieldset className="choice">
            <legend className="field-label">Applies to intents (none: every intent)</legend>
            <div className="checks">
              {options.map((o) => (
                <label key={o.id} className={clsx("check", o.proposed && "check--proposed")}>
                  <input type="checkbox" checked={section.intents.includes(o.id)} onChange={(e) => toggleIntent(o.id, e.target.checked)} />
                  {o.label}
                  {o.proposed ? <span className="muted"> (proposed)</span> : null}
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset className="choice">
            <legend className="field-label">At stages (none: every stage)</legend>
            <div className="checks">
              {STAGES.map((s) => (
                <label key={s} className="check">
                  <input type="checkbox" checked={section.stages.includes(s)} onChange={(e) => toggleStage(s, e.target.checked)} />
                  {stageLabel(s)}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="field">
            <span className="field-label">Text, as the agent reads it</span>
            <textarea className="input sop-editor" rows={10} value={section.body} onChange={(e) => props.onChange({ body: e.target.value })} />
          </label>
        </div>
      ) : (
        <div className="sop-body sop-body--clip">{section.body}</div>
      )}
    </article>
  );
}

function ProposedIntentsPanel({ draft, onDecide }: { draft: SopDraftView; onDecide: (intentId: string, accepted: boolean | null) => void }) {
  return (
    <Panel title={`Proposed intents, ${draft.proposed_intents.length}`} flush>
      {draft.proposed_intents.length === 0 ? (
        <Empty title="No new intent">The broker's catalog covers every situation this SOP describes.</Empty>
      ) : (
        <ul className="proposals">
          {draft.proposed_intents.map((p) => {
            const state = p.accepted === true ? "accepted" : p.accepted === false ? "rejected" : "waiting";
            return (
              <li key={p.intent_id} className={clsx("proposal", `proposal--${state}`)}>
                <div className="ledger-head">
                  <span className="ledger-kind">{p.title || humanize(p.intent_id)}</span>
                  <span className={clsx("chip", state === "accepted" ? "chip--pass" : state === "rejected" ? "chip--fail" : "chip--timer-pending")}>
                    {state === "accepted" ? "Accepted" : state === "rejected" ? "Rejected" : "Waiting"}
                  </span>
                </div>
                <p className="mono cell-sub">{p.intent_id}</p>
                <p className="proposal-desc">{p.description}</p>
                {p.examples.length ? (
                  <p className="cell-sub">
                    Examples: {p.examples.map((e) => `"${e}"`).join(", ")}
                  </p>
                ) : null}
                {evidenceLines(p.evidence).map((line) => (
                  <blockquote key={line} className="quote evidence">
                    {line}
                  </blockquote>
                ))}
                <p className="cell-sub">
                  Priority {p.priority}, {p.must_act ? "always acts" : "may not act"}
                  {p.noop_eligible ? ", may end with no action" : ""}
                </p>
                <div className="row proposal-actions">
                  <button type="button" className={clsx("btn btn--small", p.accepted === true && "btn--primary")} aria-pressed={p.accepted === true} onClick={() => onDecide(p.intent_id, p.accepted === true ? null : true)}>
                    Accept
                  </button>
                  <button type="button" className="btn btn--small" aria-pressed={p.accepted === false} onClick={() => onDecide(p.intent_id, p.accepted === false ? null : false)}>
                    Reject
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function ProposedRequirementsPanel({ draft, onRemove }: { draft: SopDraftView; onRemove: (index: number) => void }) {
  return (
    <Panel title={`Proposed document requirements, ${draft.proposed_requirements.length}`} flush>
      {draft.proposed_requirements.length === 0 ? (
        <Empty title="No document requirement">This SOP asks for no document at a stage.</Empty>
      ) : (
        <>
          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">At stage</th>
                <th scope="col" className="num">Within</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {draft.proposed_requirements.map((r, index) => (
                <tr key={`${requirementLabel(r)}-${index}`}>
                  <td>
                    <span className="cell-title">{humanize(r.type_id)}</span>
                    {r.opens_obligation ? <span className="cell-sub">opens {r.opens_obligation}</span> : null}
                  </td>
                  <td>{stageLabel(r.at_stage)}</td>
                  <td className="num">{r.within_minutes ? `${r.within_minutes} min` : "none"}</td>
                  <td className="actions-cell">
                    <button type="button" className="btn btn--small btn--quiet" onClick={() => onRemove(index)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint panel-pad">Publishing adds them to the broker: at the stage, the agent opens the obligation and follows it up until the document arrives clean.</p>
        </>
      )}
    </Panel>
  );
}

function ProposedObligationsPanel(props: { obligations: ObligationDef[]; existing: string[]; onRemove: (index: number) => void }) {
  if (!props.obligations.length) return null;
  return (
    <Panel title={`Proposed obligations, ${props.obligations.length}`} flush>
      <ul className="proposals">
        {props.obligations.map((o, index) => (
          <li key={`${o.kind}-${index}`}>
            <div className="ledger-head">
              <span className="ledger-kind">{o.title || humanize(o.kind)}</span>
              <Tag tone="muted">{props.existing.includes(o.kind) ? "Changes the broker's" : "New kind"}</Tag>
            </div>
            <p className="mono cell-sub">{o.kind}</p>
            {o.description ? <p className="proposal-desc">{o.description}</p> : null}
            <p className="cell-sub">
              Follow up after {o.follow_up_minutes} min, at most {o.max_attempts} request{o.max_attempts === 1 ? "" : "s"}, then{" "}
              {o.on_exhausted.map((t) => humanize(t).toLowerCase()).join(" and ")}; the timer wakes the agent with{" "}
              <span className="mono">{o.follow_up_intent || `${o.kind}_follow_up_due`}</span>.
            </p>
            <div className="row proposal-actions">
              <button type="button" className="btn btn--small btn--quiet" onClick={() => props.onRemove(index)}>
                Remove
              </button>
            </div>
          </li>
        ))}
      </ul>
      <p className="hint panel-pad">Publishing adds them to the broker's obligation types; adjust them later on the Brokers screen.</p>
    </Panel>
  );
}

function PublishPanel(props: {
  brokerId: string;
  draft: SopDraftView;
  problems: string[];
  dirty: boolean;
  save: () => Promise<boolean>;
  onPublished: () => void;
}) {
  const toasts = useToasts();
  const [canary, setCanary] = useState(false);
  const [percent, setPercent] = useState(20);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SopPublishResult | null>(null);
  const waiting = undecided(props.draft);
  const blocked = props.problems.length > 0 || (!props.dirty && props.draft.lint.length > 0);

  async function publish() {
    setBusy(true);
    try {
      if (props.dirty && !(await props.save())) return;
      const published = await api.publishSop(props.brokerId, { percent: canary ? percent : 100 });
      setResult(published);
      props.onPublished();
      invalidate("knowledge", "brokers");
      toasts.info(
        `SOP version ${published.version.slice(0, 12)} published as config version ${published.config_version}${canary ? `, on ${percent}% of loads` : ""}.`,
      );
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Publish">
      <div className="stack">
        <label className="check check--box">
          <input type="checkbox" checked={canary} onChange={(e) => setCanary(e.target.checked)} />
          Publish as a canary
        </label>
        {canary ? (
          <label className="field">
            <span className="field-label">Share of loads on the new version: {percent}%</span>
            <input className="range" type="range" min={5} max={95} step={5} value={percent} onChange={(e) => setPercent(Number(e.target.value))} />
          </label>
        ) : null}
        <p className="hint">
          {canary
            ? `${percent}% of loads run the new config version, the rest stay on the current one; widen it or roll it back on the Brokers screen.`
            : "Every load runs the new version at once; the current one is kept for a rollback."}
          {waiting ? ` ${waiting} proposed intent${waiting === 1 ? "" : "s"} still wait${waiting === 1 ? "s" : ""} for a decision and ${waiting === 1 ? "is" : "are"} left out.` : ""}
        </p>
        <button type="button" className="btn btn--primary" disabled={busy || blocked} onClick={() => void publish()}>
          {busy ? "Publishing…" : props.dirty ? "Save and publish" : "Publish"}
        </button>
        {blocked ? <p className="field-error">Fix the problems listed with the draft first.</p> : null}
        {result ? (
          <p className="lint lint--ok">
            Published SOP version <span className="mono">{result.version.slice(0, 12)}</span>, config version {result.config_version}
            {result.lint.length ? `, with ${result.lint.length} lint note${result.lint.length === 1 ? "" : "s"}: ${result.lint.join(" ")}` : "."}
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

function VersionsPanel({ brokerId, broker }: { brokerId: string; broker: BrokerView | undefined }) {
  const { timezone, state } = useSim();
  const versions = useResource(`sop-versions:${brokerId}`, () => api.sopVersions(brokerId), ["knowledge"]);
  const list = [...(versions.data ?? [])].sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at));
  return (
    <Panel title={`Published versions${versions.data ? `, ${versions.data.length}` : ""}`} flush>
      {versions.error ? <InlineError message={versions.error} onRetry={() => void versions.reload()} /> : null}
      {versions.data && list.length === 0 ? <Empty title="Nothing published yet">Loads of this broker read the standard sections until a version is published.</Empty> : null}
      {list.length ? (
        <table className="table table--compact">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">Published</th>
              <th scope="col" className="num">Sections</th>
            </tr>
          </thead>
          <tbody>
            {list.map((v) => (
              <tr key={v.version} className={clsx(broker?.sop_version === v.version && "row--selected")}>
                <td>
                  <span className="mono">{v.version.slice(0, 12)}</span>
                  {broker?.sop_version === v.version ? <span className="cell-sub">live</span> : null}
                </td>
                <td>
                  {formatStamp(v.published_at, timezone, state?.now)}
                  <span className="cell-sub">by {v.published_by}</span>
                </td>
                <td className="num" title={v.section_ids.join(", ")}>
                  {v.section_ids.length}
                  {v.lint.length ? <span className="cell-sub text-warn">{v.lint.length} lint</span> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Panel>
  );
}

// --- the broker's evals -----------------------------------------------------------------------

function BrokerEvalsPanel({ brokerId, broker }: { brokerId: string; broker: BrokerView | undefined }) {
  const toasts = useToasts();
  const scenarios = useResource("evals:scenarios", () => api.evalScenarios(), []);
  const suites = useMemo(() => [...new Set((scenarios.data ?? []).map((s) => s.suite))].sort(), [scenarios.data]);
  const [suite, setSuite] = useState("core");
  const [mode, setMode] = useState<EvalMode>("replay");
  const [k, setK] = useState(1);
  const [costCap, setCostCap] = useState(DEFAULT_COST_CAP_USD);
  const [running, setRunning] = useState<string | null>(null);
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const activeSuite = suites.includes(suite) ? suite : suites[0] ?? suite;
  const titles = new Map((scenarios.data ?? []).map((s) => [s.id, s.title]));
  const modes = EVAL_MODES.filter((m) => m.id !== "scripted");

  async function run() {
    setRunning(`suite ${activeSuite} for ${brokerId}`);
    try {
      setSummary(await api.runBrokerEvals(brokerId, { suite: activeSuite, mode, k, cost_cap_usd: costCap }));
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setRunning(null);
    }
  }

  return (
    <div className="studio-evals">
      <Panel title={`Evals for ${broker?.name ?? brokerId}`}>
        <form
          className="filters"
          onSubmit={(e) => {
            e.preventDefault();
            void run();
          }}
        >
          <label className="field">
            <span className="field-label">Suite</span>
            <select className="input" value={activeSuite} onChange={(e) => setSuite(e.target.value)} disabled={!suites.length}>
              {(suites.length ? suites : [suite]).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="segmented">
            <legend className="field-label">Model</legend>
            {modes.map((m) => (
              <label key={m.id} className={clsx("segment", mode === m.id && "segment--on")} title={m.hint}>
                <input type="radio" name="studio-eval-mode" value={m.id} checked={mode === m.id} onChange={() => setMode(m.id)} />
                {m.label}
              </label>
            ))}
          </fieldset>
          <label className="field field--narrow">
            <span className="field-label">Trials (k)</span>
            <input className="input" type="number" min={1} max={10} value={k} onChange={(e) => setK(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
          </label>
          {mode === "live" ? (
            <label className="field field--narrow">
              <span className="field-label">Cost cap, $</span>
              <input
                className="input"
                type="number"
                min={0.01}
                max={3}
                step={0.01}
                value={costCap}
                onChange={(e) => setCostCap(Math.max(0.01, Math.min(3, Number(e.target.value) || DEFAULT_COST_CAP_USD)))}
              />
            </label>
          ) : null}
          <button type="submit" className="btn btn--primary" disabled={running !== null}>
            {running ? "Running…" : `Run suite ${activeSuite}`}
          </button>
        </form>
        <p className="hint">
          Every scenario runs with this broker as the load's broker, on its config version
          {broker ? ` ${broker.config_version}` : ""} and its published SOP. {EVAL_MODES.find((m) => m.id === mode)?.hint}
        </p>
      </Panel>
      {running || summary ? <EvalResults summary={summary} running={running} titles={titles} /> : null}
    </div>
  );
}
