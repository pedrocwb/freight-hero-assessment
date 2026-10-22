import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  conversation,
  durableRequest,
  progress,
  remaining,
  type DurableSnapshot,
} from "../lib/durable";
import { href } from "../lib/route";
import {
  effectiveSops,
  operationPath,
  stageNames,
  targetNames,
} from "../lib/workspace";

const time = (value: string) => new Date(value).toLocaleTimeString("pt-BR");
const labels: Record<string, string> = {
  sms: "SMS",
  hero_task: "Tarefa para o hero",
  email: "E-mail",
  broker_slack: "Slack do broker",
  tms_note: "Nota da carga",
  stage_update: "Mudança de etapa",
};
const riskNames: Record<string, string> = {
  high: "alto",
  medium: "médio",
  low: "baixo",
};
const obligationNames: Record<string, string> = {
  open: "Em aberto",
  satisfied: "Atendida",
  cancelled: "Cancelada",
  exhausted: "Tentativas esgotadas",
};
const roles: Record<string, string> = {
  driver: "Motorista",
  dispatcher: "Dispatcher",
  broker: "Broker",
  hero: "Hero",
};

export function Durable({
  params,
  brokerId = "aurora",
}: {
  params: URLSearchParams;
  brokerId?: string;
}) {
  const loadId = params.get("load") || "";
  const path = operationPath(brokerId, loadId);
  const [snapshot, setSnapshot] = useState<DurableSnapshot | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [now, setNow] = useState(Date.now());
  const [updated, setUpdated] = useState(0);
  const [message, setMessage] = useState("");
  const [sender, setSender] = useState("");
  const [channel, setChannel] = useState("sms");
  const [busy, setBusy] = useState("");
  const [risk, setRisk] = useState("high");
  const pendingMessage = useRef<{
    body: string;
    message_id: string;
    sender_contact_id: string;
    channel: string;
  } | null>(null);
  const pendingPing = useRef<{
    ping_id: string;
    delay_risk: string;
    occurred_at: string;
  } | null>(null);
  useEffect(() => {
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(clock);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const result = await durableRequest<DurableSnapshot>(path, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setSnapshot(result);
          setError("");
          setUpdated(Date.now());
        }
      } catch (e) {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : "Conexão interrompida.");
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 2000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [path]);
  const contacts = snapshot?.load.contacts || [];
  const contact =
    contacts.find((c) => c.id === sender) ||
    contacts.find((c) => c.role === "driver") ||
    contacts[0];
  const actualChannel = contact?.channels.includes(channel as never)
    ? channel
    : contact?.channels[0] || "sms";
  async function act(suffix: string, body?: unknown) {
    setBusy(suffix);
    setActionError("");
    setNotice("");
    try {
      await durableRequest(`${path}/${suffix}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      setSnapshot(await durableRequest<DurableSnapshot>(path));
      setNotice(
        suffix === "stop"
          ? "Parada solicitada. Aguarde a confirmação."
          : "Entrada recebida. Acompanhe a decisão do agente abaixo.",
      );
      return true;
    } catch (e) {
      setActionError(
        e instanceof Error ? e.message : "Não foi possível enviar.",
      );
      return false;
    } finally {
      setBusy("");
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!contact || !message.trim()) return;
    const body = message.trim();
    if (
      pendingMessage.current?.body !== body ||
      pendingMessage.current?.sender_contact_id !== contact.id ||
      pendingMessage.current?.channel !== actualChannel
    )
      pendingMessage.current = {
        body,
        message_id: crypto.randomUUID(),
        sender_contact_id: contact.id,
        channel: actualChannel,
      };
    if (await act("messages", pendingMessage.current)) {
      setMessage("");
      pendingMessage.current = null;
    }
  }
  async function ping() {
    if (pendingPing.current?.delay_risk !== risk)
      pendingPing.current = {
        ping_id: crypto.randomUUID(),
        delay_risk: risk,
        occurred_at: new Date().toISOString(),
      };
    if (
      await act("pings", {
        ...pendingPing.current,
        load_id: loadId,
        lat: 37.9577,
        lon: -121.2908,
      })
    )
      pendingPing.current = null;
  }
  const state = snapshot ? progress(snapshot) : null;
  const halted = !!snapshot?.load.automation_stopped_at;
  const stopping = !!snapshot?.load.automation_stop_requested_at && !halted;
  const disabled = !snapshot || !!busy || !!error || halted || stopping;
  const frozen = snapshot?.load.sop_snapshot;
  const scenario = frozen?.scenario;
  const eta = frozen?.broker?.obligations.find((o) => o.kind === "eta_request");
  const status = error
    ? "Conexão interrompida"
    : halted
      ? "Execução parada"
      : stopping
        ? "Parando execução…"
        : state?.failed
          ? "Precisa de atenção"
          : snapshot?.processing
            ? "Entrada em processamento"
            : state?.complete
              ? "Escalada concluída"
              : state?.timer
                ? "Aguardando retorno"
                : state?.obligation?.status === "satisfied"
                  ? "Previsão recebida"
                  : "Monitorando a carga";
  const rows = snapshot ? conversation(snapshot) : [];
  const sections = frozen?.sections
    ? effectiveSops(frozen.sections, snapshot?.load.load_types?.[0])
    : [];
  return (
    <section className="durable" aria-label="Operação ao vivo">
      <div className="durable-heading">
        <div>
          <a className="text-link" href={href("durable", { broker: brokerId })}>
            ← Todas as operações
          </a>
          <h2>{scenario?.title || "Operação ao vivo"}</h2>
          <p className="muted">
            {snapshot?.load.shipper} · {snapshot?.load.commodity}
          </p>
        </div>
        <div className="durable-controls">
          <button
            className="button"
            onClick={() => void act("stop")}
            disabled={disabled}
          >
            {stopping || busy === "stop" ? "Parando…" : "Parar execução"}
          </button>
          <a
            className="button button--primary"
            href={href("scenarios", { broker: brokerId })}
          >
            Novo cenário
          </a>
        </div>
      </div>
      <p className="durable-mode">
        IA ao vivo · Temporal · Relógio real · Comunicações registradas em
        ambiente de teste
      </p>
      {error && (
        <p className="banner banner--alert" role="alert">
          {error} Exibindo a última atualização; reconectando automaticamente.
        </p>
      )}
      {actionError && (
        <p className="banner banner--alert" role="alert">
          {actionError}
        </p>
      )}
      {notice && (
        <p className="banner" role="status">
          {notice}
        </p>
      )}
      <div className="durable-status" role="status">
        <div>
          <span className="durable-status-dot" />
          <strong>{status}</strong>
          <p>
            {halted
              ? "Timers e ações pendentes cancelados. O histórico foi preservado."
              : stopping
                ? "Aguardando a etapa em andamento terminar antes de confirmar a parada."
                : state?.complete
                  ? "Os contatos previstos no SOP foram registrados e a obrigação foi encerrada."
                  : `${stageNames[snapshot?.load.stage || ""] || "Carregando…"} · Risco ${riskNames[snapshot?.load.delay_risk || ""] || "—"}`}
          </p>
        </div>
        <div className="durable-countdown">
          <span>{state?.timer ? "Próximo timer em" : "Pedidos de ETA"}</span>
          <strong>
            {state?.timer && !error
              ? remaining(state.timer.due_at, now)
              : `${state?.sms.length || 0}/${state?.maxAttempts || "—"}`}
          </strong>
          {state?.timer && (
            <small>
              {time(state.timer.due_at)}
              {Date.parse(state.timer.due_at) <= now
                ? " · aguardando processamento"
                : " · horário local"}
            </small>
          )}
        </div>
      </div>
      {state?.obligation && (
        <ol className="durable-attempts" aria-label="Tentativas de contato">
          {Array.from({ length: state.maxAttempts }, (_, i) => (
            <li key={i} className={state.sms[i] ? "is-sent" : ""}>
              <span>Pedido {i + 1}</span>
              <strong>
                {state.sms[i]
                  ? "Registrado"
                  : halted || state.obligation?.status !== "open"
                    ? "Encerrado"
                    : "Aguardando"}
              </strong>
              <time>
                {state.sms[i]?.accepted_at
                  ? time(state.sms[i]!.accepted_at!)
                  : "—"}
              </time>
            </li>
          ))}
        </ol>
      )}
      <div className="durable-columns">
        <section className="durable-thread" aria-labelledby="live-conversation">
          <h2 id="live-conversation">Entradas e ações do agente</h2>
          <form className="durable-composer" onSubmit={(e) => void send(e)}>
            <div className="input-sender">
              <label>
                Enviar como
                <select
                  value={contact?.id || ""}
                  disabled={disabled}
                  onChange={(e) => setSender(e.target.value)}
                >
                  {contacts.map((c) => (
                    <option key={c.id} value={c.id}>
                      {roles[c.role] || c.role} · {c.id}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Canal
                <select
                  disabled={disabled}
                  value={actualChannel}
                  onChange={(e) => setChannel(e.target.value)}
                >
                  {contact?.channels
                    .filter((c) => c !== "system")
                    .map((c) => (
                      <option key={c} value={c}>
                        {c.toUpperCase()}
                      </option>
                    ))}
                </select>
              </label>
            </div>
            <label htmlFor="driver-message">Mensagem recebida</label>
            <textarea
              id="driver-message"
              rows={3}
              maxLength={2000}
              value={message}
              placeholder="Ex.: Chego em 20 minutos. / Qual é o número do PO?"
              onChange={(e) => setMessage(e.target.value)}
              disabled={disabled}
            />
            <div>
              <small>
                A entrada é processada pelo agente desta carga, com o SOP fixado
                no início.
              </small>
              <button
                className="button button--primary"
                disabled={disabled || !message.trim()}
              >
                {busy === "messages" ? "Enviando…" : "Enviar mensagem"}
              </button>
            </div>
          </form>
          <details className="live-ping">
            <summary>Enviar location ping</summary>
            <div>
              <label>
                Risco de atraso
                <select value={risk} onChange={(e) => setRisk(e.target.value)}>
                  <option value="high">Alto</option>
                  <option value="medium">Médio</option>
                  <option value="low">Baixo</option>
                </select>
              </label>
              <button
                className="button"
                disabled={disabled}
                onClick={() => void ping()}
              >
                Enviar ping
              </button>
            </div>
            <small>
              O filtro de localização pode exigir confirmações antes de reduzir
              o risco.
            </small>
          </details>
          {rows.length === 0 && (
            <p className="durable-empty">
              O ping inicial foi enviado. Use as entradas do roteiro ou envie
              uma mensagem para acompanhar o agente.
            </p>
          )}
          {rows.map((row) => {
            if (row.kind === "driver") {
              const person = contacts.find(
                (c) => c.id === row.event.sender_contact_id,
              );
              return (
                <article
                  className="durable-entry durable-entry--driver"
                  key={row.key}
                >
                  <div className="durable-entry-meta">
                    <strong>
                      {person
                        ? roles[person.role] || person.role
                        : "Mensagem recebida"}
                    </strong>
                    <span>
                      {row.event.channel?.toUpperCase()} · {time(row.at)}
                    </span>
                  </div>
                  <p>{row.event.body}</p>
                  {row.event.attachments?.map((a) => (
                    <small key={a.attachment_id}>Anexo: {a.filename}</small>
                  ))}
                </article>
              );
            }
            const item = row.item;
            const run = snapshot?.runs.find((r) => r.run_id === item.run_id);
            return (
              <article
                className={`durable-entry ${item.kind === "hero_task" ? "durable-entry--hero" : ""}`}
                key={row.key}
              >
                <div className="durable-entry-meta">
                  <strong>{labels[item.kind] || item.kind}</strong>
                  <span>
                    {item.status === "sent"
                      ? "Registrado"
                      : item.status === "failed"
                        ? "Falhou"
                        : item.status === "cancelled"
                          ? "Cancelado"
                          : "Pendente"}{" "}
                    · {time(row.at)}
                  </span>
                </div>
                {typeof item.payload.title === "string" && (
                  <h3>{item.payload.title}</h3>
                )}
                <p>
                  {String(
                    item.payload.text ||
                      item.payload.description ||
                      item.payload.body ||
                      stageNames[String(item.payload.stage)] ||
                      item.payload.stage ||
                      "",
                  )}
                </p>
                {run && (
                  <details>
                    <summary>Ver decisão do agente</summary>
                    <p>{run.plan?.rationale}</p>
                    <ul>
                      {run.trajectory.map((step, i) => (
                        <li key={i}>
                          <code>{step.tool}</code>
                          {step.accepted === false ? " · recusado" : ""}
                        </li>
                      ))}
                    </ul>
                    {run.trace_url?.startsWith("https://") && (
                      <a href={run.trace_url} target="_blank" rel="noreferrer">
                        Abrir no LangSmith
                      </a>
                    )}
                  </details>
                )}
              </article>
            );
          })}
          {!!snapshot?.runs.length && (
            <details className="live-run-list">
              <summary>Todas as decisões ({snapshot.runs.length})</summary>
              {snapshot.runs.map((run) => (
                <article key={run.run_id}>
                  <strong>
                    {run.status} · {time(run.started_at)}
                  </strong>
                  <p>
                    {run.plan?.rationale || run.error || "Sem ação externa."}
                  </p>
                  <small>{run.trajectory.map((t) => t.tool).join(" → ")}</small>
                </article>
              ))}
            </details>
          )}
        </section>
        <aside className="durable-policy">
          <h2>Roteiro e contexto</h2>
          {scenario && (
            <>
              <p>
                As mensagens abaixo são entradas manuais do cenário. As esperas
                são cumpridas pelos timers reais do SOP; os horários originais
                são apenas referência. Os horários de ETA e as datas dos
                comprovantes de exemplo são adaptados para esta carga.
              </p>
              <ol className="live-script">
                {scenario.steps.map((step, i) => (
                  <li key={i}>
                    <small>
                      Etapa {i + 1} · {step.at}
                    </small>
                    <p>{step.note}</p>
                    {step.message && (
                      <blockquote>
                        {step.message.body}
                        {step.message.attachments.map((a) => (
                          <small key={a.filename}>{a.filename}</small>
                        ))}
                      </blockquote>
                    )}
                    {i === 0 && step.ping ? (
                      <span className="live-badge">Ping inicial enviado</span>
                    ) : step.message || step.ping ? (
                      <button
                        className="button"
                        disabled={disabled}
                        onClick={() => void act(`steps/${i}`)}
                      >
                        Enviar esta entrada
                      </button>
                    ) : (
                      <small>Aguardar o timer do SOP</small>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}
          <h3>Obrigações</h3>
          {snapshot?.obligations.length ? (
            snapshot.obligations.map((o) => (
              <p key={o.obligation_id}>
                <strong>
                  {o.kind === "eta_request" ? "Previsão de chegada" : o.kind}
                </strong>
                <br />
                {obligationNames[o.status] || o.status} · {o.attempts}{" "}
                tentativa(s)
              </p>
            ))
          ) : (
            <p>Nenhuma obrigação aberta.</p>
          )}
          <h3>SOP desta carga</h3>
          <p>
            Versão {frozen?.version || "—"} · revisão{" "}
            {frozen?.profile_revision || "anterior"}
          </p>
          <p>
            ETA: até {state?.maxAttempts} pedido(s), a cada{" "}
            {state?.intervalMinutes} minuto(s).{" "}
            {eta?.on_exhausted.map((t) => targetNames[t]).join(" → ")}
          </p>
          <details>
            <summary>Consultar SOP usado</summary>
            {sections.length ? (
              sections.map((s) => (
                <details key={s.section_id}>
                  <summary>{s.title}</summary>
                  <pre className="durable-sop-text">{s.body}</pre>
                </details>
              ))
            ) : (
              <pre className="durable-sop-text">{frozen?.policy?.body}</pre>
            )}
          </details>
          <dl>
            <dt>Carga</dt>
            <dd>{loadId}</dd>
            <dt>Execuções do agente</dt>
            <dd>{snapshot?.runs.length || 0}</dd>
            <dt>Timers disparados</dt>
            <dd>
              {snapshot?.timers.filter((t) => t.status === "fired").length || 0}
            </dd>
            <dt>Última atualização</dt>
            <dd>{updated ? time(new Date(updated).toISOString()) : "—"}</dd>
          </dl>
          <p>
            Fechar a página não interrompe a execução. Para encerrar, use “Parar
            execução”.
          </p>
        </aside>
      </div>
    </section>
  );
}
