import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";

import { ApiError, api, STAGES, STANDARD_BROKER_ID, type BrokerView, type Intent, type ResolvedSection, type Stage } from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag } from "../components/ui";
import { useBrokerList } from "../hooks/useBrokers";
import { useResource } from "../hooks/useResource";
import { humanize, stageLabel } from "../lib/load";
import { useSim } from "../state/sim";

/** The broker's catalog; a client the API does not know as a broker reads the standard one. */
async function catalogOf(clientId: string): Promise<BrokerView> {
  try {
    return await api.getBroker(clientId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && clientId !== STANDARD_BROKER_ID) {
      return api.getBroker(STANDARD_BROKER_ID);
    }
    throw error;
  }
}

/** The text after the person stops typing for a moment. */
function useSettled(value: string, ms = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

function parseTypes(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/**
 * Which layer a source comes from: a file under sops/standard/, a load type's file, or
 * the broker's own (its client files, or a document its published SOP was compiled from).
 */
function layerOf(source: string): "standard" | "broker" | "load type" {
  if (source.includes("/types/")) return "load type";
  if (source.includes("standard/")) return "standard";
  return "broker";
}

export function SopViewer() {
  const { load, loads } = useSim();
  const brokers = useBrokerList();
  // Every broker the API knows, plus the clients of loads that are not brokers (yet).
  const clients = useMemo(() => {
    const names = new Map<string, string>();
    for (const b of brokers.data ?? []) names.set(b.broker_id, b.name);
    for (const l of loads) if (!names.has(l.broker_id)) names.set(l.broker_id, l.broker_name);
    return [...names.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.id.localeCompare(b.id));
  }, [brokers.data, loads]);
  const [client, setClient] = useState(load?.broker_id ?? "northline");
  const [typesText, setTypesText] = useState(load?.load_types.join(", ") ?? "");
  const [intent, setIntent] = useState<Intent>("delay_risk_high");
  const [stage, setStage] = useState<Stage>(load?.stage ?? "to_pickup");
  const [picked, setPicked] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [query, setQuery] = useState("");
  const settledQuery = useSettled(query.trim());

  // Follow the selected load until the person changes a field.
  useEffect(() => {
    if (!load || touched) return;
    setClient(load.broker_id);
    setTypesText(load.load_types.join(", "));
    setStage(load.stage);
  }, [load, touched]);

  const types = parseTypes(typesText);
  const typesKey = types.join(",");
  const broker = useResource(`broker:${client}`, () => catalogOf(client), ["brokers"]);
  const intents = useMemo(() => [...(broker.data?.intents ?? [])].sort((a, b) => a.priority - b.priority), [broker.data]);
  const intentKey = intents.map((i) => i.intent_id).join(",");

  // Another broker may not have the chosen intent: fall back to its first high-risk or message intent.
  useEffect(() => {
    const ids = intentKey ? intentKey.split(",") : [];
    if (!ids.length || ids.includes(intent)) return;
    setIntent(ids.includes("delay_risk_high") ? "delay_risk_high" : ids[0]!);
  }, [intentKey, intent]);

  const effective = useResource(`sops:${client}:${typesKey}`, () => api.sops(client, types), ["knowledge"]);
  const hits = useResource(
    settledQuery ? `search:${client}:${typesKey}:${settledQuery}` : null,
    () => api.searchSops({ brokerId: client, loadTypes: types, q: settledQuery, k: 5 }),
    ["knowledge"],
  );
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
        lede="The procedure the agent reads for a broker, load type, event intent and stage. Standard sections apply until a broker's published SOP replaces or extends them."
        actions={
          effective.data ? (
            <span className="version" title="The version of the broker's published SOP the agent reads">
              SOP version <span className="mono">{effective.data.version.slice(0, 12)}</span>
            </span>
          ) : null
        }
      />
      <Panel title="Situation">
        {brokers.error ? <InlineError message={brokers.error} onRetry={() => void brokers.reload()} /> : null}
        <form className="filters" onSubmit={(e) => e.preventDefault()}>
          <label className="field">
            <span className="field-label">Broker</span>
            <select className="input" value={client} onChange={(e) => change(setClient)(e.target.value)}>
              {clients.some((c) => c.id === client) ? null : <option value={client}>{client}</option>}
              {clients.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id === c.name ? c.id : `${c.name} (${c.id})`}
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
            <select
              className="input"
              value={intent}
              onChange={(e) => change(setIntent)(e.target.value as Intent)}
              disabled={!intents.length}
            >
              {!intents.length ? <option value={intent}>{broker.loading ? "Loading the catalog…" : humanize(intent)}</option> : null}
              <optgroup label="Messages">
                {intents
                  .filter((i) => !i.system)
                  .map((i) => (
                    <option key={i.intent_id} value={i.intent_id}>
                      {i.title || humanize(i.intent_id)}
                    </option>
                  ))}
              </optgroup>
              <optgroup label="Set by code">
                {intents
                  .filter((i) => i.system)
                  .map((i) => (
                    <option key={i.intent_id} value={i.intent_id}>
                      {i.title || humanize(i.intent_id)}
                    </option>
                  ))}
              </optgroup>
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
        {broker.error ? <InlineError message={broker.error} onRetry={() => void broker.reload()} /> : null}
        {preview.data && explicit.length === 0 ? (
          <p className="banner banner--warn" role="status">
            No section names this intent at this stage. The agent starts from the general sections
            and searches the SOP; if nothing covers the event, it hands it to a Hero with "no SOP
            for this situation".
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
          <Panel title="Search the SOP" flush>
            <form className="panel-pad search-form" role="search" onSubmit={(e) => e.preventDefault()}>
              <label className="field">
                <span className="field-label">What the agent would search for</span>
                <input
                  className="input"
                  type="search"
                  value={query}
                  placeholder="e.g. trailer temperature seal"
                  onChange={(e) => setQuery(e.target.value)}
                />
              </label>
            </form>
            {hits.error ? <InlineError message={hits.error} onRetry={() => void hits.reload()} /> : null}
            {settledQuery && hits.data && hits.data.length === 0 ? (
              <Empty title="No section matches">Try other words: the search reads titles, summaries and text.</Empty>
            ) : null}
            {hits.data?.length ? (
              <ol className="catalog" aria-label="Search results, best first">
                {hits.data.map((hit) => (
                  <li key={hit.section_id}>
                    <button
                      type="button"
                      className={clsx("catalog-item", picked === hit.section_id && "catalog-item--on")}
                      aria-pressed={picked === hit.section_id}
                      onClick={() => setPicked(picked === hit.section_id ? null : hit.section_id)}
                    >
                      <span className="catalog-title">
                        {hit.title || hit.section_id}
                        <span className="tag tag--muted" title="Relevance">{hit.score.toFixed(2)}</span>
                      </span>
                      <span className="mono catalog-id">{hit.section_id}</span>
                      {hit.description ? <span className="catalog-desc">{hit.description}</span> : null}
                    </button>
                  </li>
                ))}
              </ol>
            ) : null}
            {!settledQuery ? (
              <p className="hint panel-pad search-hint">
                The same search the agent's <span className="mono">search_sop</span> tool runs, over
                every section this broker's loads of these types see.
              </p>
            ) : null}
          </Panel>
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
