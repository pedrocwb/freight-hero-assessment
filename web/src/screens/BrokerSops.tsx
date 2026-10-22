import { useEffect, useState } from "react";
import { durableRequest } from "../lib/durable";
import {
  brokerPath,
  effectiveSops,
  renderSop,
  targetNames,
  type BrokerProfile,
  type SopSection,
} from "../lib/workspace";

export function BrokerSops({
  profile,
  onSaved,
}: {
  profile: BrokerProfile;
  onSaved: (profile: BrokerProfile) => void;
}) {
  const storageKey = `broker-sop-draft:${profile.broker.broker_id}`;
  const [draft, setDraft] = useState<BrokerProfile>(() => {
    try {
      const saved = JSON.parse(
        localStorage.getItem(storageKey) || "null",
      ) as BrokerProfile | null;
      return saved?.revision === profile.revision &&
        saved.broker.broker_id === profile.broker.broker_id
        ? saved
        : profile;
    } catch {
      return profile;
    }
  });
  const [scope, setScope] = useState("");
  const [topic, setTopic] = useState("delay_risk_high");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const dirty = JSON.stringify(draft) !== JSON.stringify(profile);
  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(draft));
  }, [draft, storageKey]);
  const visible = effectiveSops(draft.sections, scope);
  const selected = visible.find((s) => s.section_id === topic);
  const ownIndex = draft.sections.findIndex(
    (s) =>
      s.section_id === topic &&
      s.layer === (scope ? "load_type" : "client") &&
      (s.load_type || "") === scope,
  );
  const own = draft.sections[ownIndex];
  const inherited = effectiveSops(
    draft.sections.filter((_, i) => i !== ownIndex),
    scope,
  ).find((s) => s.section_id === topic);
  const types = [
    ...new Set([
      "reefer",
      ...draft.sections.flatMap((s) => (s.load_type ? [s.load_type] : [])),
    ]),
  ];
  function edit(change: Partial<SopSection>) {
    if (!own) return;
    setDraft({
      ...draft,
      sections: draft.sections.map((s, i) =>
        i === ownIndex ? { ...s, ...change } : s,
      ),
    });
    setStatus("");
  }
  function customize(mode: "replace" | "extend") {
    if (!selected) return;
    const section: SopSection = {
      ...selected,
      client_id: draft.broker.broker_id,
      layer: scope ? "load_type" : "client",
      load_type: scope || null,
      source: "workspace",
      mode,
      body: mode === "extend" ? "" : selected.body,
    };
    setDraft({ ...draft, sections: [...draft.sections, section] });
  }
  function addTopic() {
    let id = "novo_procedimento";
    while (draft.sections.some((s) => s.section_id === id)) id += "_novo";
    setDraft({
      ...draft,
      sections: [
        ...draft.sections,
        {
          section_id: id,
          layer: scope ? "load_type" : "client",
          client_id: draft.broker.broker_id,
          load_type: scope || null,
          title: "Novo procedimento",
          description: "",
          intents: [],
          stages: [],
          mode: "replace",
          body: "Descreva quando o agente deve agir e quais passos deve seguir.",
          source: "workspace",
        },
      ],
    });
    setTopic(id);
  }
  async function save() {
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const result = await durableRequest<BrokerProfile>(
        `${brokerPath(profile.broker.broker_id)}/profile`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(draft),
        },
      );
      localStorage.removeItem(storageKey);
      setDraft(result);
      onSaved(result);
      setStatus("SOP salvo. Novas cargas usarão esta versão.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível salvar.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="workspace-content sop-workspace">
      <div className="workspace-intro">
        <div>
          <h2>SOPs e configurações</h2>
          <p>
            Padrão → regras do broker → tipo de carga. Edite em texto livre e
            escolha substituir ou estender.
          </p>
        </div>
        <div className="durable-controls">
          <span className="muted">
            {dirty
              ? "Alterações não publicadas"
              : `Versão ${profile.revision} salva`}
          </span>
          <button
            className="button button--primary"
            onClick={() => void save()}
            disabled={busy || !dirty}
          >
            {busy ? "Salvando…" : "Salvar para novas execuções"}
          </button>
        </div>
      </div>
      <p className="sop-version-note">
        Cargas em andamento mantêm seu SOP original. O rascunho é guardado neste
        navegador até você salvar.
      </p>
      {status && (
        <p role="status" className="banner">
          {status}
        </p>
      )}
      {error && (
        <p role="alert" className="banner banner--alert">
          {error}
        </p>
      )}
      <fieldset className="sop-settings" disabled={busy}>
        <legend>Regras de acompanhamento deste broker</legend>
        {draft.broker.obligations.map((o, index) => (
          <div className="obligation-config" key={o.kind}>
            <strong>{o.title}</strong>
            <label>
              Tentativas
              <input
                type="number"
                min={1}
                max={10}
                value={o.max_attempts}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    broker: {
                      ...draft.broker,
                      obligations: draft.broker.obligations.map((v, i) =>
                        i === index
                          ? { ...v, max_attempts: Number(e.target.value) }
                          : v,
                      ),
                    },
                  })
                }
              />
            </label>
            <label>
              Intervalo (minutos)
              <input
                type="number"
                min={1}
                max={10080}
                value={o.follow_up_minutes}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    broker: {
                      ...draft.broker,
                      obligations: draft.broker.obligations.map((v, i) =>
                        i === index
                          ? { ...v, follow_up_minutes: Number(e.target.value) }
                          : v,
                      ),
                    },
                  })
                }
              />
            </label>
            <div>
              <span>Escalada sem resposta</span>
              <div className="escalation-options">
                {Object.entries(targetNames).map(([id, name]) => (
                  <label key={id}>
                    <input
                      type="checkbox"
                      checked={o.on_exhausted.includes(id)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          broker: {
                            ...draft.broker,
                            obligations: draft.broker.obligations.map((v, i) =>
                              i === index
                                ? {
                                    ...v,
                                    on_exhausted: e.target.checked
                                      ? [...v.on_exhausted, id]
                                      : v.on_exhausted.filter((t) => t !== id),
                                  }
                                : v,
                            ),
                          },
                        })
                      }
                    />
                    {name}
                  </label>
                ))}
              </div>
              <small>
                Ordem:{" "}
                {o.on_exhausted.map((t) => targetNames[t]).join(" → ") ||
                  "Selecione ao menos um destino"}
              </small>
            </div>
          </div>
        ))}
      </fieldset>
      <div className="sop-toolbar">
        <label>
          Aplicar a
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">Todas as cargas do broker</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {t === "reefer" ? "Cargas refrigeradas (reefer)" : t}
              </option>
            ))}
          </select>
        </label>
        <button className="button" onClick={addTopic} disabled={busy}>
          + Novo procedimento
        </button>
      </div>
      <div className="sop-editor-layout">
        <nav className="sop-topic-list" aria-label="Procedimentos">
          {visible.map((s) => (
            <button
              key={s.section_id}
              className={topic === s.section_id ? "is-current" : ""}
              onClick={() => setTopic(s.section_id)}
            >
              <strong>{s.title}</strong>
              <small>{s.section_id}</small>
            </button>
          ))}
        </nav>
        <div className="sop-topic-editor">
          {selected && (
            <>
              <div className="workspace-intro">
                <div>
                  <h3>{selected.title}</h3>
                  <p>
                    {own
                      ? `${scope ? "Tipo de carga" : "Broker"} · ${own.mode === "extend" ? "extensão" : "substituição"}`
                      : "Herdado do SOP padrão"}
                  </p>
                </div>
                {own && (
                  <button
                    className="button"
                    disabled={busy}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        sections: draft.sections.filter(
                          (_, i) => i !== ownIndex,
                        ),
                      })
                    }
                  >
                    Usar regra herdada
                  </button>
                )}
              </div>
              {!own && (
                <>
                  <div className="durable-controls">
                    <button
                      className="button"
                      onClick={() => customize("replace")}
                    >
                      Substituir para este cliente
                    </button>
                    <button
                      className="button button--primary"
                      onClick={() => customize("extend")}
                    >
                      Adicionar uma extensão
                    </button>
                  </div>
                  <pre className="sop-preview">
                    {renderSop(selected.body, draft)}
                  </pre>
                </>
              )}
              {own && (
                <fieldset disabled={busy}>
                  <div className="sop-fields">
                    <label>
                      Título
                      <input
                        disabled={own.mode === "extend"}
                        value={own.title}
                        onChange={(e) => edit({ title: e.target.value })}
                      />
                    </label>
                    <label>
                      Como combinar
                      <select
                        value={own.mode}
                        onChange={(e) =>
                          edit({ mode: e.target.value as "extend" | "replace" })
                        }
                      >
                        <option value="replace">
                          Substituir o procedimento herdado
                        </option>
                        {inherited && (
                          <option value="extend">
                            Estender o procedimento herdado
                          </option>
                        )}
                      </select>
                    </label>
                  </div>
                  <label>
                    Descrição curta
                    <input
                      disabled={own.mode === "extend"}
                      value={own.description}
                      onChange={(e) => edit({ description: e.target.value })}
                    />
                  </label>
                  <label htmlFor="broker-sop-text">Texto do SOP</label>
                  <textarea
                    id="broker-sop-text"
                    rows={16}
                    value={own.body}
                    maxLength={20000}
                    onChange={(e) => edit({ body: e.target.value })}
                  />
                  <small>
                    Os campos {"{{eta_request.max_attempts}}"},{" "}
                    {"{{eta_request.follow_up_minutes}}"} e{" "}
                    {"{{eta_request.on_exhausted}}"} acompanham as configurações
                    acima.
                  </small>
                  <details>
                    <summary>Quando este procedimento se aplica</summary>
                    <p>
                      Em extensões, os eventos são herdados do procedimento
                      base. Em substituições, não selecionar eventos
                      disponibiliza o procedimento para todos.
                    </p>
                    <div className="intent-options">
                      {draft.broker.intents.map((intent) => (
                        <label key={intent.intent_id}>
                          <input
                            type="checkbox"
                            disabled={own.mode === "extend"}
                            checked={own.intents.includes(intent.intent_id)}
                            onChange={(e) =>
                              edit({
                                intents: e.target.checked
                                  ? [...own.intents, intent.intent_id]
                                  : own.intents.filter(
                                      (id) => id !== intent.intent_id,
                                    ),
                              })
                            }
                          />
                          {intent.title}
                        </label>
                      ))}
                    </div>
                  </details>
                </fieldset>
              )}
              <details className="sop-effective">
                <summary>Ver o texto final que o agente receberá</summary>
                <pre className="sop-preview">
                  {renderSop(selected.body, draft)}
                </pre>
              </details>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
