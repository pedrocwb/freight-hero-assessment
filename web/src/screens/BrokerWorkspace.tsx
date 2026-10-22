import { useEffect, useState } from "react";
import { durableRequest, type DurableHealth } from "../lib/durable";
import { href, type Route } from "../lib/route";
import {
  brokerPath,
  stageNames,
  targetNames,
  type BrokerInfo,
  type BrokerProfile,
  type LiveLoad,
  type LiveScenario,
} from "../lib/workspace";
import { Durable } from "./Durable";
import { BrokerSops } from "./BrokerSops";

export function BrokerWorkspace({
  brokerId,
  route,
}: {
  brokerId: string;
  route: Route;
}) {
  const [brokers, setBrokers] = useState<BrokerInfo[]>([]);
  const [profile, setProfile] = useState<BrokerProfile | null>(null);
  const [scenarios, setScenarios] = useState<LiveScenario[]>([]);
  const [loads, setLoads] = useState<LiveLoad[]>([]);
  const [health, setHealth] = useState<DurableHealth | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState("");
  const [search, setSearch] = useState("");
  const [reload, setReload] = useState(0);
  const view = route.screen;
  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    void Promise.all([
      durableRequest<BrokerInfo[]>("/workspace/brokers", options),
      durableRequest<BrokerProfile>(`${brokerPath(brokerId)}/profile`, options),
      durableRequest<LiveScenario[]>(
        `${brokerPath(brokerId)}/scenarios`,
        options,
      ),
      durableRequest<DurableHealth>("/health", options),
    ])
      .then(([b, p, s, h]) => {
        if (!controller.signal.aborted) {
          setBrokers(b);
          setProfile(p);
          setScenarios(s);
          setHealth(h);
          setError("");
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e.message));
      });
    return () => controller.abort();
  }, [brokerId, reload]);
  useEffect(() => {
    let timer: number;
    const controller = new AbortController();
    async function poll() {
      try {
        const rows = await durableRequest<LiveLoad[]>(
          `${brokerPath(brokerId)}/loads`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted)
          setLoads(
            rows.sort((a, b) =>
              (b.started_at || "").localeCompare(a.started_at || ""),
            ),
          );
      } catch {
        /* Startup error is reported by the workspace request. */
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 5000);
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [brokerId, route.params.get("load")]);

  async function start(scenario: LiveScenario) {
    if (!profile || starting) return;
    setStarting(scenario.id);
    setError("");
    const storageKey = `live-start:${brokerId}:${scenario.id}:${profile.revision}`;
    const id =
      sessionStorage.getItem(storageKey) ||
      `${brokerId}-${crypto.randomUUID()}`;
    sessionStorage.setItem(storageKey, id);
    try {
      await durableRequest(`${brokerPath(brokerId)}/loads/${id}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario_id: scenario.id,
          revision: profile.revision,
        }),
      });
      sessionStorage.removeItem(storageKey);
      window.location.hash = href("durable", { broker: brokerId, load: id });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível iniciar.");
    } finally {
      setStarting("");
    }
  }
  const selected = brokers.find((b) => b.id === brokerId);
  const eta = profile?.broker.obligations.find((o) => o.kind === "eta_request");
  const visible = scenarios.filter((s) =>
    `${s.title} ${s.description} ${s.suite} ${s.load_types.join(" ")}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <div className="workspace">
      <a className="skip-link" href="#main">
        Ir para o conteúdo
      </a>
      <aside className="broker-sidebar">
        <a
          className="workspace-brand"
          href={href("scenarios", { broker: brokerId })}
        >
          <span className="brand-mark" />
          Load agent
        </a>
        <p className="eyebrow">Clientes / brokers</p>
        <nav aria-label="Brokers">
          {brokers.map((b, i) => (
            <a
              key={b.id}
              href={href("scenarios", { broker: b.id })}
              aria-current={b.id === brokerId ? "page" : undefined}
              className={`broker-link ${b.id === brokerId ? "is-current" : ""}`}
            >
              <span className="broker-monogram">
                {["N", "K", "A", "AC"][i]}
              </span>
              <span>
                <strong>{b.name}</strong>
                <small>{b.description}</small>
              </span>
            </a>
          ))}
        </nav>
        <div className="workspace-runtime">
          <span className="durable-status-dot" />
          Temporal · tempo real
          <p>As operações continuam com esta página fechada.</p>
        </div>
      </aside>
      <main id="main" className="workspace-main">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">Ambiente de testes dos agentes</p>
            <h1>{selected?.name || brokerId}</h1>
            <p>{selected?.description || "Carregando broker…"}</p>
          </div>
          <span className="live-badge">● Operações ao vivo</span>
        </header>
        <nav className="workspace-tabs" aria-label="Área do broker">
          {(
            [
              { id: "scenarios", label: "Cenários" },
              { id: "durable", label: "Operações" },
              { id: "sops", label: "SOPs e configurações" },
            ] as const
          ).map((tab) => (
            <a
              key={tab.id}
              className={view === tab.id ? "is-current" : ""}
              aria-current={view === tab.id ? "page" : undefined}
              href={href(tab.id, { broker: brokerId })}
            >
              {tab.label}
              {tab.id === "scenarios" && <span>{scenarios.length}</span>}
            </a>
          ))}
        </nav>
        {error && (
          <div className="banner banner--alert" role="alert">
            {error}{" "}
            <button className="button" onClick={() => setReload((n) => n + 1)}>
              Recarregar
            </button>
          </div>
        )}
        {health && !health.model_ready && (
          <p className="banner banner--warn">
            Configure a IA para iniciar as operações.
          </p>
        )}
        {view === "scenarios" && (
          <section className="workspace-content">
            <div className="workspace-intro">
              <div>
                <h2>Escolha o cenário. Acompanhe o agente.</h2>
                <p>
                  Um location ping inicia cada carga. Envie as entradas pela UI;
                  os timers do SOP disparam no tempo real. Os roteiros são
                  referências: os limites acima valem para novas execuções.
                </p>
              </div>
              <a
                className="text-link"
                href={href("sops", { broker: brokerId })}
              >
                Editar SOP do broker →
              </a>
            </div>
            <div className="policy-strip">
              <div>
                <small>Pedidos de ETA</small>
                <strong>{eta?.max_attempts ?? "—"} tentativa(s)</strong>
              </div>
              <div>
                <small>Espera após cada pedido</small>
                <strong>{eta?.follow_up_minutes ?? "—"} minutos</strong>
              </div>
              <div>
                <small>Escalada sem resposta</small>
                <strong>
                  {eta?.on_exhausted.map((t) => targetNames[t]).join(" → ") ||
                    "—"}
                </strong>
              </div>
              <div>
                <small>SOP salvo</small>
                <strong>Versão {profile?.revision ?? "—"}</strong>
              </div>
            </div>
            <div className="scenario-toolbar">
              <h3>{visible.length} cenários disponíveis</h3>
              <input
                aria-label="Buscar cenário"
                type="search"
                placeholder="Buscar cenário, evento ou tipo de carga"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="live-scenario-grid">
              {visible.map((s) => (
                <article key={s.id} className="live-scenario-card">
                  <div className="scenario-card-meta">
                    <span>{s.suite}</span>
                    {s.load_types.map((t) => (
                      <span key={t}>{t === "reefer" ? "Refrigerada" : t}</span>
                    ))}
                  </div>
                  <h3>{s.title}</h3>
                  <p>{s.description}</p>
                  <div className="scenario-card-foot">
                    <small>
                      {stageNames[s.stage] || s.stage} · {s.steps.length} etapas
                    </small>
                    <button
                      className="button button--primary"
                      disabled={!!starting || !health?.model_ready}
                      onClick={() => void start(s)}
                    >
                      {starting === s.id ? "Iniciando…" : "Iniciar ao vivo"}
                    </button>
                  </div>
                </article>
              ))}
            </div>
            {!visible.length && profile && (
              <p className="durable-empty">
                Nenhum cenário encontrado para esta busca.
              </p>
            )}
          </section>
        )}
        {view === "sops" && profile && (
          <BrokerSops key={brokerId} profile={profile} onSaved={setProfile} />
        )}
        {view === "durable" &&
          (route.params.get("load") ? (
            <Durable
              key={`${brokerId}:${route.params.get("load")}`}
              params={route.params}
              brokerId={brokerId}
            />
          ) : (
            <section className="workspace-content">
              <div className="workspace-intro">
                <div>
                  <h2>Histórico de operações</h2>
                  <p>
                    Cada carga tem seu próprio workflow e sua versão do SOP.
                  </p>
                </div>
                <a
                  className="button button--primary"
                  href={href("scenarios", { broker: brokerId })}
                >
                  Iniciar cenário
                </a>
              </div>
              <div className="live-load-list">
                {loads.map((load) => (
                  <a
                    key={load.load_id}
                    href={href("durable", {
                      broker: brokerId,
                      load: load.load_id,
                    })}
                  >
                    <div>
                      <strong>{load.scenario}</strong>
                      <small>{load.load_id}</small>
                    </div>
                    <span>
                      {load.stopped
                        ? "Parada"
                        : load.stopping
                          ? "Parando…"
                          : stageNames[load.stage] || load.stage}
                    </span>
                    <small>
                      {load.started_at
                        ? new Date(load.started_at).toLocaleString("pt-BR")
                        : "Execução anterior"}{" "}
                      →
                    </small>
                  </a>
                ))}
              </div>
              {!loads.length && (
                <p className="durable-empty">
                  Ainda não há operações neste broker. Escolha um cenário para
                  começar.
                </p>
              )}
            </section>
          ))}
      </main>
    </div>
  );
}
