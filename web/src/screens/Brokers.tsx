import clsx from "clsx";
import { useEffect, useState, type ReactNode } from "react";

import {
  api,
  brokerInput,
  ESCALATION_TARGETS,
  STAGES,
  STANDARD_BROKER_ID,
  type Broker,
  type BrokerLimits,
  type BrokerView,
  type DocumentRequirement,
  type EscalationTarget,
  type IntentDef,
  type ObligationDef,
  type Stage,
} from "../api";
import { Modal } from "../components/Modal";
import { Empty, InlineError, Meter, Panel, ScreenHead, Tag, usd } from "../components/ui";
import { useBrokerChoice, useBrokerList } from "../hooks/useBrokers";
import { useResource } from "../hooks/useResource";
import {
  brokerProblems,
  followUpIntentOf,
  isSnakeId,
  linesToList,
  newIntent,
  newObligation,
  newRequirement,
  removeAt,
  replaceAt,
  rolloutSummary,
  sameJson,
} from "../lib/broker";
import { invalidate } from "../lib/bus";
import { humanize, stageLabel } from "../lib/load";
import { useToasts } from "../state/toasts";

const TARGET_LABEL: Record<EscalationTarget, string> = {
  dispatcher_email: "Email the dispatcher",
  hero_task: "Hero task",
  broker_slack: "Broker Slack",
};

export function Brokers() {
  const brokers = useBrokerList();
  const [chosen, choose] = useBrokerChoice(brokers.data);
  const [creating, setCreating] = useState(false);

  return (
    <>
      <ScreenHead
        title="Brokers"
        lede="Each broker's catalog, as data: the intents the agent reads events as, what it owes contacts, the documents it expects, its limits and the SOP version its loads run. Saving makes a new config version."
        actions={
          <button type="button" className="btn btn--primary" onClick={() => setCreating(true)}>
            New broker
          </button>
        }
      />
      <div className="grid grid--brokers">
        <div className="col-main">
          {chosen ? (
            <BrokerProfile key={chosen} brokerId={chosen} />
          ) : (
            <Panel title="Broker">
              <Empty title={brokers.loading ? "Loading the brokers…" : "No broker yet"}>
                {brokers.loading ? null : "Create one: it starts as a copy of the standard broker."}
              </Empty>
            </Panel>
          )}
        </div>
        <div className="col-side">
          <BrokerList brokers={brokers.data} error={brokers.error} onRetry={() => void brokers.reload()} chosen={chosen} onChoose={choose} />
          {chosen ? <RolloutPanel key={`rollout-${chosen}`} brokerId={chosen} /> : null}
        </div>
      </div>
      <NewBrokerDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(broker) => {
          choose(broker.broker_id);
          setCreating(false);
        }}
      />
    </>
  );
}

function BrokerList(props: {
  brokers: BrokerView[] | undefined;
  error: string | null;
  onRetry: () => void;
  chosen: string | null;
  onChoose: (id: string) => void;
}) {
  const list = [...(props.brokers ?? [])].sort(
    (a, b) => Number(b.broker_id === STANDARD_BROKER_ID) - Number(a.broker_id === STANDARD_BROKER_ID) || a.name.localeCompare(b.name),
  );
  return (
    <Panel title={`Brokers${props.brokers ? `, ${props.brokers.length}` : ""}`} flush>
      {props.error ? <InlineError message={props.error} onRetry={props.onRetry} /> : null}
      <ul className="catalog">
        {list.map((b) => (
          <li key={b.broker_id}>
            <button
              type="button"
              className={clsx("catalog-item", props.chosen === b.broker_id && "catalog-item--on")}
              aria-pressed={props.chosen === b.broker_id}
              onClick={() => props.onChoose(b.broker_id)}
            >
              <span className="catalog-title">
                {b.name}
                {b.broker_id === STANDARD_BROKER_ID ? <Tag tone="muted">Template</Tag> : null}
                {b.rollout.percent < 100 && b.rollout.previous_version !== null ? <Tag tone="urgent">Canary {b.rollout.percent}%</Tag> : null}
              </span>
              <span className="mono catalog-id">{b.broker_id}</span>
              <span className="catalog-desc">
                Config v{b.config_version}, {b.intents?.length ?? 0} intents, SOP{" "}
                {b.sop_version ? <span className="mono">{b.sop_version.slice(0, 8)}</span> : "standard"}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

// --- the profile -------------------------------------------------------------------------

function BrokerProfile({ brokerId }: { brokerId: string }) {
  const toasts = useToasts();
  const resource = useResource(`broker:${brokerId}`, () => api.getBroker(brokerId), ["brokers"]);
  const [edit, setEdit] = useState<Broker | null>(null);
  const [saving, setSaving] = useState(false);
  const server = resource.data;
  const broker = edit ?? server;

  if (!broker) {
    return (
      <Panel title={brokerId}>
        {resource.error ? <InlineError message={resource.error} onRetry={() => void resource.reload()} /> : <p className="muted">Loading the broker…</p>}
      </Panel>
    );
  }

  const dirty = edit !== null && server !== undefined && !sameJson(brokerInput(edit), brokerInput(server));
  const problems = dirty ? brokerProblems(broker) : [];
  const update = (patch: Partial<Broker>) => setEdit({ ...broker, ...patch });

  async function save() {
    if (!edit) return;
    setSaving(true);
    try {
      const saved = await api.updateBroker(brokerId, brokerInput(edit));
      setEdit(null);
      invalidate("brokers", "knowledge");
      toasts.info(`${saved.name} saved as config version ${saved.config_version}.`);
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      {dirty ? (
        <div className="save-bar" role="region" aria-label="Unsaved changes">
          <div>
            <p className="strong">Unsaved changes to version {broker.config_version}</p>
            <p className="hint">Saving makes version {broker.config_version + 1}, live for every load at once; the Rollout panel can make it a canary.</p>
            {problems.length ? (
              <ul className="problems">
                {problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            ) : null}
          </div>
          <div className="save-bar-actions">
            <button type="button" className="btn" onClick={() => setEdit(null)} disabled={saving}>
              Discard
            </button>
            <button type="button" className="btn btn--primary" onClick={() => void save()} disabled={saving || problems.length > 0}>
              {saving ? "Saving…" : `Save as version ${broker.config_version + 1}`}
            </button>
          </div>
        </div>
      ) : null}

      <Panel
        title={
          <>
            {broker.name || broker.broker_id}
            <span className="panel-title-sub mono">{broker.broker_id}</span>
          </>
        }
        actions={<span className="chip chip--stage">Config v{broker.config_version}</span>}
      >
        <div className="row">
          <label className="field">
            <span className="field-label">Name</span>
            <input className="input" value={broker.name} onChange={(e) => update({ name: e.target.value })} />
          </label>
          <div className="field">
            <span className="field-label">Triage</span>
            <label className="check check--box">
              <input type="checkbox" checked={broker.triage} onChange={(e) => update({ triage: e.target.checked })} />
              Read each message with the gate model first
            </label>
          </div>
        </div>
        <dl className="facts profile-facts">
          <div>
            <dt>Published SOP</dt>
            <dd>
              {broker.sop_version ? <span className="mono">{broker.sop_version.slice(0, 12)}</span> : <span className="muted">none: the standard sections</span>}{" "}
              <a href="#/studio">Open the SOP studio</a>
            </dd>
          </div>
          <div>
            <dt>Rollout</dt>
            <dd>{rolloutSummary(broker.config_version, broker.rollout)}</dd>
          </div>
          <div>
            <dt>Catalog</dt>
            <dd>
              {broker.intents.filter((i) => !i.system).length} message intents, {broker.intents.filter((i) => i.system).length} set by code,{" "}
              {broker.obligations.length} obligation type{broker.obligations.length === 1 ? "" : "s"}
            </dd>
          </div>
        </dl>
        {!broker.triage ? (
          <p className="hint">Triage is off: every message goes straight to the agent with its intent unset, and the agent labels it.</p>
        ) : null}
      </Panel>

      <IntentsPanel intents={broker.intents} onChange={(intents) => update({ intents })} />
      <ObligationsPanel
        obligations={broker.obligations}
        intents={broker.intents}
        onChange={(obligations, intents) => update(intents ? { obligations, intents } : { obligations })}
      />
      <DocumentsPanel broker={broker} onChange={(documents) => update({ documents })} />
      <LimitsPanel limits={broker.limits} obligations={broker.obligations} onChange={(limits) => update({ limits })} />
    </>
  );
}

function IntentsPanel({ intents, onChange }: { intents: IntentDef[]; onChange: (intents: IntentDef[]) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [newId, setNewId] = useState("");
  const ordered = intents.map((def, index) => ({ def, index })).sort((a, b) => Number(a.def.system) - Number(b.def.system) || a.def.priority - b.def.priority);
  const set = (index: number, patch: Partial<IntentDef>) => onChange(replaceAt(intents, index, patch));
  const idProblem = newId && !isSnakeId(newId) ? "Use snake_case, like detention_request." : intents.some((i) => i.intent_id === newId) ? "This intent exists." : null;

  return (
    <Panel title={`Intents, ${intents.length}`} flush>
      <div className="table-wrap">
        <table className="table table--compact def-table">
          <thead>
            <tr>
              <th scope="col">Intent</th>
              <th scope="col" className="num" title="Lower wins when a message fits more than one intent">
                Priority
              </th>
              <th scope="col" title="Always decide, whatever the gate says">
                Must act
              </th>
              <th scope="col" title="May end with no action">
                No-op eligible
              </th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          {ordered.map(({ def, index }) => {
            const isOpen = open === def.intent_id;
            return (
              <tbody key={def.intent_id} className={clsx(def.system && "def--system")}>
                <tr>
                  <td>
                    <span className="cell-title">
                      {def.title || humanize(def.intent_id)}
                      {def.system ? <Tag tone="muted">Set by code</Tag> : null}
                    </span>
                    <span className="cell-sub mono">{def.intent_id}</span>
                  </td>
                  <td className="num">
                    <input
                      className="input input--small input--num"
                      type="number"
                      min={0}
                      value={def.priority}
                      disabled={def.system}
                      aria-label={`Priority of ${def.intent_id}`}
                      onChange={(e) => set(index, { priority: Number(e.target.value) || 0 })}
                    />
                  </td>
                  <td>
                    <input type="checkbox" className="checkbox" checked={def.must_act} disabled={def.system} aria-label={`${def.intent_id} must act`} onChange={(e) => set(index, { must_act: e.target.checked })} />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      className="checkbox"
                      checked={def.noop_eligible}
                      disabled={def.system}
                      aria-label={`${def.intent_id} may end with no action`}
                      onChange={(e) => set(index, { noop_eligible: e.target.checked })}
                    />
                  </td>
                  <td className="actions-cell">
                    <button type="button" className="btn btn--small" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : def.intent_id)}>
                      {isOpen ? "Close" : def.system ? "View" : "Edit"}
                    </button>
                    {!def.system ? (
                      <button type="button" className="btn btn--small btn--quiet" onClick={() => onChange(removeAt(intents, index))}>
                        Remove
                      </button>
                    ) : null}
                  </td>
                </tr>
                {isOpen ? (
                  <tr className="def-detail">
                    <td colSpan={5}>
                      {def.system ? (
                        <div className="stack">
                          <p>{def.description}</p>
                          <p className="hint">The code sets this intent from the event (a ping, a timer, the document pipeline); it cannot be changed here.</p>
                        </div>
                      ) : (
                        <div className="form-grid">
                          <label className="field">
                            <span className="field-label">Title</span>
                            <input className="input" value={def.title} onChange={(e) => set(index, { title: e.target.value })} />
                          </label>
                          <label className="field form-grid-wide">
                            <span className="field-label">Description, as the gate and the agent read it</span>
                            <textarea className="input" rows={3} value={def.description} onChange={(e) => set(index, { description: e.target.value })} />
                          </label>
                          <label className="field form-grid-wide">
                            <span className="field-label">Examples, one per line</span>
                            <textarea
                              className="input"
                              rows={3}
                              value={def.examples.join("\n")}
                              onChange={(e) => set(index, { examples: linesToList(e.target.value) })}
                            />
                          </label>
                        </div>
                      )}
                    </td>
                  </tr>
                ) : null}
              </tbody>
            );
          })}
        </table>
      </div>
      <form
        className="panel-pad add-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newId || idProblem) return;
          onChange([...intents, newIntent(newId)]);
          setOpen(newId);
          setNewId("");
        }}
      >
        <label className="field">
          <span className="field-label">New message intent</span>
          <input className="input mono" value={newId} placeholder="detention_request" onChange={(e) => setNewId(e.target.value.trim())} aria-invalid={idProblem ? true : undefined} />
        </label>
        <button type="submit" className="btn" disabled={!newId || Boolean(idProblem)}>
          Add intent
        </button>
        {idProblem ? <p className="field-error">{idProblem}</p> : null}
      </form>
    </Panel>
  );
}

function ObligationsPanel(props: {
  obligations: ObligationDef[];
  intents: IntentDef[];
  onChange: (obligations: ObligationDef[], intents?: IntentDef[]) => void;
}) {
  const { obligations, intents } = props;
  const [newKind, setNewKind] = useState("");
  const set = (index: number, patch: Partial<ObligationDef>) => props.onChange(replaceAt(obligations, index, patch));
  const kindProblem = newKind && !isSnakeId(newKind) ? "Use snake_case, like pod." : obligations.some((o) => o.kind === newKind) ? "This kind exists." : null;
  const toggle = (index: number, target: EscalationTarget, on: boolean) => {
    const current = obligations[index]!.on_exhausted;
    set(index, { on_exhausted: on ? ESCALATION_TARGETS.filter((t) => t === target || current.includes(t)) : current.filter((t) => t !== target) });
  };

  return (
    <Panel title={`Obligation types, ${obligations.length}`} flush>
      <div className="table-wrap">
        <table className="table table--compact def-table">
          <thead>
            <tr>
              <th scope="col">What the agent waits for</th>
              <th scope="col" className="num">Follow-up, min</th>
              <th scope="col" className="num">Max attempts</th>
              <th scope="col">Follow-up intent</th>
              <th scope="col">When the attempts run out</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {obligations.map((o, index) => {
              const followUp = followUpIntentOf(o);
              const registered = intents.some((i) => i.intent_id === followUp);
              return (
                <tr key={`${o.kind}-${index}`}>
                  <td>
                    <input className="input input--small input--wide" value={o.title} aria-label={`Title of ${o.kind}`} onChange={(e) => set(index, { title: e.target.value })} />
                    <span className="cell-sub mono">{o.kind}</span>
                    <input
                      className="input input--small input--wide def-desc"
                      value={o.description}
                      placeholder="What it means for the agent"
                      aria-label={`Description of ${o.kind}`}
                      onChange={(e) => set(index, { description: e.target.value })}
                    />
                  </td>
                  <td className="num">
                    <input className="input input--small input--num" type="number" min={1} value={o.follow_up_minutes} aria-label={`Follow-up minutes of ${o.kind}`} onChange={(e) => set(index, { follow_up_minutes: Number(e.target.value) || 0 })} />
                  </td>
                  <td className="num">
                    <input className="input input--small input--num" type="number" min={1} value={o.max_attempts} aria-label={`Max attempts of ${o.kind}`} onChange={(e) => set(index, { max_attempts: Number(e.target.value) || 0 })} />
                  </td>
                  <td>
                    <input
                      className="input input--small input--wide mono"
                      value={o.follow_up_intent}
                      placeholder={`${o.kind}_follow_up_due`}
                      aria-label={`Follow-up intent of ${o.kind}`}
                      onChange={(e) => set(index, { follow_up_intent: e.target.value.trim() })}
                    />
                    {!registered ? <span className="cell-sub text-warn">{followUp} is not in the catalog</span> : null}
                  </td>
                  <td>
                    <div className="checks">
                      {ESCALATION_TARGETS.map((t) => (
                        <label key={t} className="check">
                          <input type="checkbox" checked={o.on_exhausted.includes(t)} onChange={(e) => toggle(index, t, e.target.checked)} />
                          {TARGET_LABEL[t]}
                        </label>
                      ))}
                    </div>
                  </td>
                  <td className="actions-cell">
                    <button type="button" className="btn btn--small btn--quiet" onClick={() => props.onChange(removeAt(obligations, index))}>
                      Remove
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <form
        className="panel-pad add-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newKind || kindProblem) return;
          const def = newObligation(newKind);
          const followUp = followUpIntentOf(def);
          // A follow-up timer wakes the agent with a system intent, so the catalog needs it.
          const extra = intents.some((i) => i.intent_id === followUp)
            ? undefined
            : [
                ...intents,
                {
                  ...newIntent(followUp),
                  title: `${humanize(newKind)} follow-up due`,
                  description: `The follow-up timer of an open ${newKind} obligation came due. Set by code, no gate call.`,
                  system: true,
                },
              ];
          props.onChange([...obligations, def], extra);
          setNewKind("");
        }}
      >
        <label className="field">
          <span className="field-label">New obligation kind</span>
          <input className="input mono" value={newKind} placeholder="pod" onChange={(e) => setNewKind(e.target.value.trim())} aria-invalid={kindProblem ? true : undefined} />
        </label>
        <button type="submit" className="btn" disabled={!newKind || Boolean(kindProblem)}>
          Add obligation type
        </button>
        {kindProblem ? <p className="field-error">{kindProblem}</p> : <p className="hint">Its follow-up intent joins the catalog as a system intent.</p>}
      </form>
    </Panel>
  );
}

function DocumentsPanel({ broker, onChange }: { broker: Broker; onChange: (documents: DocumentRequirement[]) => void }) {
  const set = (index: number, patch: Partial<DocumentRequirement>) => onChange(replaceAt(broker.documents, index, patch));
  return (
    <Panel title={`Documents, ${broker.document_types.length} types and ${broker.documents.length} requirement${broker.documents.length === 1 ? "" : "s"}`} flush>
      <div className="table-wrap">
        <table className="table table--compact">
          <caption className="visually-hidden">Document types</caption>
          <thead>
            <tr>
              <th scope="col">Type</th>
              <th scope="col">Fields the pipeline reads, and the check against the load</th>
            </tr>
          </thead>
          <tbody>
            {broker.document_types.map((t) => (
              <tr key={t.type_id}>
                <td>
                  <span className="cell-title">{t.title}</span>
                  <span className="cell-sub mono">{t.type_id}</span>
                </td>
                <td>
                  <p className="cell-desc">{t.description}</p>
                  <div className="tag-list">
                    {t.fields.map((f) => (
                      <span key={f.name} className="tag" title={`${f.description} (${f.kind})`}>
                        <span className="mono">{f.name}</span>
                        {f.check !== "none" ? <span className="tag-check">checked: {f.check.replace(/_/g, " ")}</span> : null}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="subhead panel-pad-x">Requirements</h3>
      {broker.documents.length === 0 ? (
        <Empty title="No document is required">
          Add one, or accept the requirements a SOP draft proposes: at a stage, the agent opens an
          obligation and follows it up until the document arrives clean.
        </Empty>
      ) : (
        <div className="table-wrap">
          <table className="table table--compact">
            <caption className="visually-hidden">Document requirements</caption>
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">At stage</th>
                <th scope="col" className="num">Within, min</th>
                <th scope="col">Opens obligation</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {broker.documents.map((r, index) => (
                <tr key={index}>
                  <td>
                    <select className="input input--small" value={r.type_id} aria-label="Document type" onChange={(e) => set(index, { type_id: e.target.value })}>
                      {broker.document_types.some((t) => t.type_id === r.type_id) ? null : <option value={r.type_id}>{r.type_id}</option>}
                      {broker.document_types.map((t) => (
                        <option key={t.type_id} value={t.type_id}>
                          {t.title}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select className="input input--small" value={r.at_stage} aria-label="Stage" onChange={(e) => set(index, { at_stage: e.target.value as Stage })}>
                      {STAGES.map((s) => (
                        <option key={s} value={s}>
                          {stageLabel(s)}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="num">
                    <input
                      className="input input--small input--num"
                      type="number"
                      min={1}
                      value={r.within_minutes ?? ""}
                      placeholder="none"
                      aria-label="Within minutes"
                      onChange={(e) => set(index, { within_minutes: e.target.value === "" ? null : Number(e.target.value) })}
                    />
                  </td>
                  <td>
                    <select
                      className="input input--small"
                      value={r.opens_obligation ?? ""}
                      aria-label="Obligation it opens"
                      onChange={(e) => set(index, { opens_obligation: e.target.value || null })}
                    >
                      <option value="">none</option>
                      {r.opens_obligation && !broker.obligations.some((o) => o.kind === r.opens_obligation) ? (
                        <option value={r.opens_obligation}>{r.opens_obligation} (unknown)</option>
                      ) : null}
                      {broker.obligations.map((o) => (
                        <option key={o.kind} value={o.kind}>
                          {o.kind}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="actions-cell">
                    <button type="button" className="btn btn--small btn--quiet" onClick={() => onChange(removeAt(broker.documents, index))}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="panel-pad">
        <button type="button" className="btn" onClick={() => onChange([...broker.documents, newRequirement(broker)])}>
          Add requirement
        </button>
      </div>
    </Panel>
  );
}

function NumberField(props: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  hint?: ReactNode;
  onChange: (value: number) => void;
}) {
  return (
    <label className="field">
      <span className="field-label">{props.label}</span>
      <input
        className="input"
        type="number"
        min={props.min}
        max={props.max}
        step={props.step ?? 1}
        value={Number.isFinite(props.value) ? props.value : ""}
        onChange={(e) => props.onChange(Number(e.target.value))}
      />
      {props.hint ? <span className="hint">{props.hint}</span> : null}
    </label>
  );
}

function LimitsPanel(props: { limits: BrokerLimits; obligations: ObligationDef[]; onChange: (limits: BrokerLimits) => void }) {
  const { limits } = props;
  const v = limits.validation;
  const setV = (patch: Partial<BrokerLimits["validation"]>) => props.onChange({ ...limits, validation: { ...v, ...patch } });
  const setDeferral = (patch: Partial<BrokerLimits["validation"]["deferral"]>) => setV({ deferral: { ...v.deferral, ...patch } });
  return (
    <Panel title="Limits">
      <h3 className="subhead subhead--first">Agent loop budget, per run</h3>
      <div className="row">
        <NumberField
          label="Max steps"
          min={0}
          max={20}
          value={limits.max_steps}
          onChange={(n) => props.onChange({ ...limits, max_steps: n })}
          hint={limits.max_steps === 0 ? "0: single-plan mode, the plan comes in the answer with no action tools" : "Tool rounds; 0 is the single-plan mode"}
        />
        <NumberField label="Max cost, $" min={0.001} step={0.005} value={limits.max_cost_usd} onChange={(n) => props.onChange({ ...limits, max_cost_usd: n })} hint={`${usd(limits.max_cost_usd, 3)}: the adapter forces the answer past it`} />
        <NumberField label="Max rejections" min={0} value={limits.max_rejections} onChange={(n) => props.onChange({ ...limits, max_rejections: n })} hint="More refused calls than this blocks the run and calls a Hero" />
      </div>
      <h3 className="subhead">Validator</h3>
      <div className="row">
        <NumberField label="SMS per person per window" min={1} value={v.sms_max_per_window} onChange={(n) => setV({ sms_max_per_window: n })} />
        <NumberField label="SMS window, min" min={1} value={v.sms_window_minutes} onChange={(n) => setV({ sms_window_minutes: n })} />
      </div>
      <div className="row">
        <NumberField label="Longest wait, min" min={0} value={v.deferral.max_minutes} onChange={(n) => setDeferral({ max_minutes: n })} hint="A promise of the ETA later" />
        <NumberField label="Waits per obligation" min={0} value={v.deferral.max_per_obligation} onChange={(n) => setDeferral({ max_per_obligation: n })} />
        <NumberField label="Grace after a wait, min" min={0} value={v.deferral.grace_minutes} onChange={(n) => setDeferral({ grace_minutes: n })} />
      </div>
      <p className="hint">
        Attempt caps come from the obligation types:{" "}
        {props.obligations.map((o, i) => (
          <span key={o.kind}>
            {i > 0 ? ", " : ""}
            <span className="mono">{o.kind}</span> {o.max_attempts}
          </span>
        ))}
        .
      </p>
    </Panel>
  );
}

// --- rollout and versions ------------------------------------------------------------------

function RolloutPanel({ brokerId }: { brokerId: string }) {
  const toasts = useToasts();
  const broker = useResource(`broker:${brokerId}`, () => api.getBroker(brokerId), ["brokers"]);
  const versions = useResource(`broker-versions:${brokerId}`, () => api.brokerVersions(brokerId), ["brokers"]);
  const data = broker.data;
  const [percent, setPercent] = useState<number | null>(null);
  const [previous, setPrevious] = useState<number | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);

  // A saved change (a new version, a publish) resets what the person was setting up.
  useEffect(() => {
    setPercent(null);
    setPrevious(undefined);
  }, [data?.config_version, data?.rollout.percent, data?.rollout.previous_version]);

  if (!data) {
    return (
      <Panel title="Rollout">
        {broker.error ? <InlineError message={broker.error} onRetry={() => void broker.reload()} /> : <p className="muted">Loading…</p>}
      </Panel>
    );
  }

  const older = (versions.data ?? [])
    .map((v) => v.config_version)
    .filter((n) => n !== data.config_version)
    .sort((a, b) => b - a);
  const share = percent ?? data.rollout.percent;
  const prev = previous === undefined ? data.rollout.previous_version : previous;
  const changed = share !== data.rollout.percent || prev !== data.rollout.previous_version;

  async function apply(next: { percent: number; previous_version: number | null }) {
    setBusy(true);
    try {
      const saved = await api.setRollout(brokerId, next);
      invalidate("brokers");
      toasts.info(rolloutSummary(saved.config_version, saved.rollout));
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Panel title="Rollout" actions={<span className="chip chip--stage">v{data.config_version} live</span>}>
        <div className="stack">
          <Meter label={`Loads on version ${data.config_version}`} value={share} max={100} valueText={`${share}%`} />
          <label className="field">
            <span className="field-label">Share of loads on version {data.config_version}</span>
            <input
              className="range"
              type="range"
              min={0}
              max={100}
              step={5}
              value={share}
              disabled={prev === null || busy}
              onChange={(e) => setPercent(Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span className="field-label">The rest run version</span>
            <select
              className="input"
              value={prev ?? ""}
              disabled={busy || !older.length}
              onChange={(e) => setPrevious(e.target.value === "" ? null : Number(e.target.value))}
            >
              <option value="">none</option>
              {older.map((n) => (
                <option key={n} value={n}>
                  Version {n}
                </option>
              ))}
            </select>
          </label>
          <p className="rollout-summary">{rolloutSummary(data.config_version, { percent: share, previous_version: prev })}</p>
          <div className="row">
            <button type="button" className="btn btn--primary" disabled={!changed || busy} onClick={() => void apply({ percent: share, previous_version: prev })}>
              {busy ? "Applying…" : "Apply rollout"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || prev === null || (data.rollout.percent === 0 && !changed)}
              onClick={() => void apply({ percent: 0, previous_version: prev })}
            >
              {prev === null ? "Roll back" : `Roll back to v${prev}`}
            </button>
          </div>
          <p className="hint">
            A load keeps the version it drew for its whole life (a hash of its id), so a canary is
            sticky. Rolling back is 0%: every load runs the previous version again.
          </p>
        </div>
      </Panel>
      <Panel title="Config versions" flush>
        {versions.error ? <InlineError message={versions.error} onRetry={() => void versions.reload()} /> : null}
        {versions.data && versions.data.length === 0 ? <Empty title="No version recorded" /> : null}
        {versions.data?.length ? (
          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Catalog</th>
                <th scope="col">SOP</th>
              </tr>
            </thead>
            <tbody>
              {[...versions.data]
                .sort((a, b) => b.config_version - a.config_version)
                .map((v) => (
                  <tr key={v.config_version} className={clsx(v.current && "row--selected")}>
                    <td>
                      v{v.config_version}
                      {v.current ? <span className="cell-sub">current</span> : v.config_version === data.rollout.previous_version ? <span className="cell-sub">previous</span> : null}
                    </td>
                    <td>
                      {v.intents.length} intents, {v.obligations.length} obligation{v.obligations.length === 1 ? "" : "s"}
                      <span className="cell-sub">{v.obligations.join(", ")}</span>
                    </td>
                    <td className="mono">{v.sop_version ? v.sop_version.slice(0, 8) : <span className="muted">standard</span>}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        ) : null}
      </Panel>
    </>
  );
}

// --- create ----------------------------------------------------------------------------------

function NewBrokerDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (broker: BrokerView) => void }) {
  const toasts = useToasts();
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const idProblem = id && !isSnakeId(id) ? "Use snake_case, starting with a letter: acme, acme_west." : null;

  async function create() {
    setBusy(true);
    try {
      const broker = await api.createBroker({ broker_id: id, name: name.trim() });
      invalidate("brokers");
      toasts.info(`${broker.name} created as a copy of the standard broker, config version ${broker.config_version}.`);
      setId("");
      setName("");
      onCreated(broker);
    } catch (error) {
      toasts.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="New broker"
      submitLabel={busy ? "Creating…" : "Create broker"}
      submitDisabled={busy || !id || !name.trim() || Boolean(idProblem)}
      onClose={onClose}
      onSubmit={() => void create()}
    >
      <p className="hint">
        It starts as a copy of the standard broker: the intents, the ETA request, the four document
        types and the default limits. Upload its SOP in the SOP studio next.
      </p>
      <label className="field">
        <span className="field-label">Broker id (the loads' broker.id)</span>
        <input className="input mono" value={id} placeholder="acme" onChange={(e) => setId(e.target.value.trim())} aria-invalid={idProblem ? true : undefined} />
      </label>
      {idProblem ? <p className="field-error">{idProblem}</p> : null}
      <label className="field">
        <span className="field-label">Name</span>
        <input className="input" value={name} placeholder="Acme Freight" onChange={(e) => setName(e.target.value)} />
      </label>
    </Modal>
  );
}
