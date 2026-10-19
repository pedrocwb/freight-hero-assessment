import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";

import { api, INTENTS, STAGES, type Intent, type ResolvedSection, type Stage } from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { humanize, stageLabel } from "../lib/load";
import { useSim } from "../state/sim";

// The clients the SOP tree defines (docs/decisions.md); loads may add others.
const KNOWN_CLIENTS = ["northline", "keystone"];

function parseTypes(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Which layer a source file comes from, read from its path under sops/. */
function layerOf(source: string): "standard" | "client" | "load type" {
  if (source.includes("/types/")) return "load type";
  if (source.includes("clients/")) return "client";
  return "standard";
}

export function SopViewer() {
  const { load, loads } = useSim();
  const clients = useMemo(
    () => [...new Set([...KNOWN_CLIENTS, ...loads.map((l) => l.broker_id)])].sort(),
    [loads],
  );
  const [client, setClient] = useState(load?.broker_id ?? "northline");
  const [typesText, setTypesText] = useState(load?.load_types.join(", ") ?? "");
  const [intent, setIntent] = useState<Intent>("delay_risk_high");
  const [stage, setStage] = useState<Stage>(load?.stage ?? "to_pickup");
  const [picked, setPicked] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  // Follow the selected load until the person changes a field.
  useEffect(() => {
    if (!load || touched) return;
    setClient(load.broker_id);
    setTypesText(load.load_types.join(", "));
    setStage(load.stage);
  }, [load, touched]);

  const types = parseTypes(typesText);
  const typesKey = types.join(",");
  const effective = useResource(`sops:${client}:${typesKey}`, () => api.sops(client, types), []);
  const preview = useResource(
    `preview:${client}:${typesKey}:${intent}:${stage}`,
    () => api.sopPreview({ clientId: client, loadTypes: types, intent, stage }),
    [],
  );

  const applies = new Set(preview.data?.sections.map((s) => s.section_id) ?? []);
  const bodies = new Map<string, ResolvedSection>((effective.data?.sections ?? []).map((s) => [s.section_id, s]));
  const catalog = preview.data?.catalog ?? [];
  const explicit = (preview.data?.sections ?? []).filter((s) => s.intents.length > 0);
  const shown = picked ? bodies.get(picked) : undefined;
  const change = <T,>(set: (v: T) => void) => (value: T) => {
    setTouched(true);
    set(value);
  };

  return (
    <>
      <ScreenHead
        title="SOP viewer"
        lede="The procedure the agent reads for a client, load type, event intent and stage. Standard sections apply until a client replaces or extends them."
        actions={
          effective.data ? (
            <span className="version" title="Hash of the SOP files the API loaded">
              SOP version <span className="mono">{effective.data.version.slice(0, 12)}</span>
            </span>
          ) : null
        }
      />
      <Panel title="Situation">
        <form className="filters" onSubmit={(e) => e.preventDefault()}>
          <label className="field">
            <span className="field-label">Client</span>
            <select className="input" value={client} onChange={(e) => change(setClient)(e.target.value)}>
              {clients.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Load types, comma separated</span>
            <input
              className="input"
              list="load-type-options"
              value={typesText}
              placeholder="none"
              onChange={(e) => change(setTypesText)(e.target.value)}
            />
            <datalist id="load-type-options">
              <option value="reefer" />
            </datalist>
          </label>
          <label className="field">
            <span className="field-label">Event intent</span>
            <select className="input" value={intent} onChange={(e) => change(setIntent)(e.target.value as Intent)}>
              {INTENTS.map((i) => (
                <option key={i} value={i}>
                  {humanize(i)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Load stage</span>
            <select className="input" value={stage} onChange={(e) => change(setStage)(e.target.value as Stage)}>
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {stageLabel(s)}
                </option>
              ))}
            </select>
          </label>
        </form>
        {preview.data && explicit.length === 0 ? (
          <p className="banner banner--warn" role="status">
            No section names this intent at this stage, so a run would escalate to a Hero with
            "no SOP for this situation". General sections still apply.
          </p>
        ) : null}
      </Panel>

      <div className="grid grid--sops">
        <section className="col-main" aria-labelledby="applies-title">
          <h2 className="section-title" id="applies-title">
            Applies to this event{preview.data ? `, ${preview.data.sections.length} sections` : ""}
          </h2>
          {preview.error ? <InlineError message={preview.error} onRetry={() => void preview.reload()} /> : null}
          {preview.data?.sections.map((section) => (
            <SectionCard key={section.section_id} section={section} />
          ))}
          {preview.data && preview.data.sections.length === 0 ? (
            <Panel title="Nothing applies">
              <Empty title="No section applies">Pick another intent or stage.</Empty>
            </Panel>
          ) : null}
        </section>
        <div className="col-side">
          <Panel title={`Catalog, ${catalog.length} sections`} flush>
            {effective.error ? <InlineError message={effective.error} onRetry={() => void effective.reload()} /> : null}
            {catalog.length === 0 && preview.data ? <Empty title="This client has no section" /> : null}
            <ul className="catalog">
              {catalog.map((entry) => (
                <li key={entry.section_id}>
                  <button
                    type="button"
                    className={clsx("catalog-item", picked === entry.section_id && "catalog-item--on")}
                    aria-pressed={picked === entry.section_id}
                    onClick={() => setPicked(picked === entry.section_id ? null : entry.section_id)}
                  >
                    <span className="catalog-title">
                      {entry.title}
                      {applies.has(entry.section_id) ? <Tag tone="sop">Applies</Tag> : null}
                    </span>
                    <span className="mono catalog-id">{entry.section_id}</span>
                    <span className="catalog-desc">{entry.description}</span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
          {shown ? <SectionCard section={shown} /> : null}
        </div>
      </div>
    </>
  );
}

function SectionCard({ section }: { section: ResolvedSection }) {
  const extended = section.sources.length > 1;
  return (
    <article className="sop" aria-labelledby={`sop-${section.section_id}`}>
      <header className="sop-head">
        <h3 className="sop-title" id={`sop-${section.section_id}`}>
          {section.title}
        </h3>
        <span className="mono sop-id">{section.section_id}</span>
      </header>
      <p className="sop-desc">{section.description}</p>
      <dl className="sop-meta">
        <div>
          <dt>Intents</dt>
          <dd>{section.intents.length ? section.intents.map(humanize).join(", ") : "every intent"}</dd>
        </div>
        <div>
          <dt>Stages</dt>
          <dd>{section.stages.length ? section.stages.map(stageLabel).join(", ") : "every stage"}</dd>
        </div>
        <div className="sop-meta-wide">
          <dt>{extended ? "Built from" : "From"}</dt>
          <dd>
            {section.sources.map((s, i) => (
              <span key={s} className="sop-source">
                {i > 0 ? (extended ? " extended by " : ", ") : null}
                <span className="mono">{s}</span> <span className="muted">({layerOf(s)})</span>
              </span>
            ))}
          </dd>
        </div>
      </dl>
      <div className="sop-body">{section.body}</div>
    </article>
  );
}
