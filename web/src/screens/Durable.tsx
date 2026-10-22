import { useEffect, useRef, useState, type FormEvent } from "react";

import { conversation, durableRequest, progress, remaining, type DurableHealth, type DurableSnapshot } from "../lib/durable";
import { href } from "../lib/route";

const time = (value: string) => new Date(value).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo" });
const labels: Record<string, string> = { sms: "SMS para o motorista", hero_task: "Tarefa para o hero", email: "E-mail", tms_note: "Nota da carga" };

export function Durable({ params }: { params: URLSearchParams }) {
  const loadId = params.get("load") || "";
  const [health, setHealth] = useState<DurableHealth | null>(null);
  const [snapshot, setSnapshot] = useState<DurableSnapshot | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [updated, setUpdated] = useState(0);
  const [lastLoad, setLastLoad] = useState(() => localStorage.getItem("aurora-last-load"));
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [sendStatus, setSendStatus] = useState("");
  const [sendError, setSendError] = useState("");
  const pendingMessage = useRef<{ body: string; message_id: string } | null>(null);

  useEffect(() => {
    setMessage("");
    setSendStatus("");
    setSendError("");
    pendingMessage.current = null;
  }, [loadId]);

  const loadedId = snapshot?.load.load_id;
  useEffect(() => {
    if (loadedId) {
      localStorage.setItem("aurora-last-load", loadedId);
      setLastLoad(loadedId);
    }
  }, [loadedId]);

  useEffect(() => {
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(clock);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    setSnapshot(null);
    setError("");
    setUpdated(0);
    const poll = async () => {
      try {
        const [status, state] = await Promise.all([
          durableRequest<DurableHealth>("/health", { signal: controller.signal }),
          loadId ? durableRequest<DurableSnapshot>(`/loads/${encodeURIComponent(loadId)}`, { signal: controller.signal }) : Promise.resolve(null),
        ]);
        if (controller.signal.aborted) return;
        setHealth(status);
        setSnapshot(state);
        setUpdated(Date.now());
        setError("");
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "A conexão foi interrompida.");
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [loadId]);

  async function start() {
    setStarting(true);
    setError("");
    // Retain the ID on an uncertain response: retrying cannot start another load.
    const pending = sessionStorage.getItem("aurora-start-id") || `aurora-${crypto.randomUUID()}`;
    sessionStorage.setItem("aurora-start-id", pending);
    try {
      const result = await durableRequest<{ load_id: string }>(`/demos/aurora/${pending}`, { method: "POST" });
      sessionStorage.removeItem("aurora-start-id");
      localStorage.setItem("aurora-last-load", result.load_id);
      window.location.hash = href("durable", { load: result.load_id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Não foi possível iniciar a carga.");
    } finally {
      setStarting(false);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const body = message.trim();
    if (!body || sending || !loadId) return;
    setSending(true);
    setSendStatus("");
    setSendError("");
    if (pendingMessage.current?.body !== body) {
      pendingMessage.current = { body, message_id: crypto.randomUUID() };
    }
    try {
      const result = await durableRequest<{ accepted: boolean; duplicate: boolean }>(`/loads/${encodeURIComponent(loadId)}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pendingMessage.current),
      });
      if (!result.accepted && !result.duplicate) throw new Error("A mensagem não foi aceita. Tente novamente.");
      pendingMessage.current = null;
      setMessage("");
      setSendStatus("Mensagem recebida pelo agente.");
    } catch (cause) {
      setSendError(cause instanceof Error ? cause.message : "Não foi possível enviar. Tente novamente.");
    } finally {
      setSending(false);
    }
  }

  const state = snapshot ? progress(snapshot) : null;
  const stopped = state?.obligation && ["satisfied", "cancelled"].includes(state.obligation.status);
  const active = !!loadId && !state?.complete && !state?.failed && !stopped;
  const status = error ? "Conexão interrompida" : state?.failed ? "A execução precisa de atenção" : state?.complete ? "Escalado para um hero" : stopped ? "Acompanhamento encerrado" : state?.timer ? "Aguardando o motorista" : loadId ? "Agente processando" : "Pronto para iniciar";
  const deliveries = [...(snapshot?.outbox || [])].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const rows = snapshot ? conversation(snapshot) : [];
  const driverReplied = rows.some((row) => row.kind === "driver");

  return (
    <section className="durable" aria-label="Operação ao vivo">
      <div className="durable-heading">
        <div>
          <h1>Aurora Transportes</h1>
          <p className="muted">Alimentos Serra Verde · Curitiba → São Paulo · 18 toneladas</p>
        </div>
        <button className="button button--primary" onClick={() => void start()} disabled={starting || active || !health?.model_ready || !!error}>
          {starting ? "Iniciando…" : loadId ? "Nova execução" : "Iniciar cenário"}
        </button>
      </div>

      <p className="durable-mode">IA ao vivo · Relógio real · SMS e tarefas registrados em ambiente de teste</p>
      {error && <p className="banner banner--alert" role="alert">{error} Os dados abaixo são da última atualização; a tela tenta reconectar automaticamente.</p>}
      {health && !health.model_ready && <p className="banner banner--warn">A IA ainda não está configurada no serviço da operação ao vivo.</p>}
      {!loadId && lastLoad && <a href={href("durable", { load: lastLoad })}>Retomar a última execução</a>}

      <div className="durable-status" role="status">
        <div><span className="durable-status-dot" /><strong>{status}</strong>
          <p>{state?.complete ? "O limite de tentativas foi atingido. O hero deve ligar para confirmar a chegada." : stopped ? "A obrigação foi encerrada e os próximos pedidos foram cancelados." : driverReplied ? "O motorista respondeu. O agente avalia a mensagem conforme o SOP." : "Risco alto de atraso na entrega. O motorista não responde aos pedidos de previsão."}</p>
        </div>
        <div className="durable-countdown">
          <span>{state?.timer ? (state.sms.length === 10 ? "Escalada em" : "Próximo contato em") : "Contatos confirmados"}</span>
          <strong>{state?.timer && !error ? remaining(state.timer.due_at, now) : `${state?.sms.length || 0}/10`}</strong>
          {state?.timer && <small>{time(state.timer.due_at)} · horário de Brasília{Date.parse(state.timer.due_at) <= now ? " · aguardando processamento" : ""}</small>}
        </div>
      </div>

      <ol className="durable-attempts" aria-label="Dez tentativas de contato">
        {Array.from({ length: 10 }, (_, i) => {
          const sms = state?.sms[i];
          return <li key={i} className={sms ? "is-sent" : ""}>
            <span>SMS {i + 1}</span><strong>{sms ? "Registrado" : "Aguardando"}</strong>
            <time>{sms?.accepted_at ? time(sms.accepted_at) : "—"}</time>
          </li>;
        })}
      </ol>

      <div className="durable-columns">
        <section className="durable-thread" aria-labelledby="durable-conversation">
          <h2 id="durable-conversation">Contatos e escalada</h2>
          <form className="durable-composer" onSubmit={(event) => void sendMessage(event)}>
            <label htmlFor="driver-message">Mensagem do motorista</label>
            <textarea id="driver-message" rows={3} maxLength={2000} value={message}
              placeholder="Ex.: Chego ao CD em 20 minutos."
              onChange={(event) => setMessage(event.target.value)} disabled={!snapshot || sending}
              aria-describedby="driver-message-hint" />
            <div><small id="driver-message-hint">Simula um SMS recebido nesta carga. Uma previsão de chegada pode encerrar os pedidos.</small>
              <button className="button button--primary" type="submit" disabled={!snapshot || !message.trim() || sending || !!error}>{sending ? "Enviando…" : "Enviar como motorista"}</button>
            </div>
            {sendStatus && <p role="status">{sendStatus}</p>}
            {sendError && <p className="banner banner--alert" role="alert">{sendError}</p>}
          </form>
          {rows.length === 0 && <p className="durable-empty">{loadId ? "O agente está preparando o primeiro contato." : "Inicie o cenário para acompanhar o primeiro pedido de previsão de chegada."}</p>}
          {rows.map((row) => {
            if (row.kind === "driver") return <article className="durable-entry durable-entry--driver" key={row.key}>
              <div className="durable-entry-meta"><strong>Motorista</strong><span>SMS recebido · {time(row.at)}</span></div>
              <p>{row.event.body}</p>
            </article>;
            const item = row.item;
            const i = deliveries.findIndex((d) => d.outbox_id === item.outbox_id);
            const run = snapshot?.runs.find((r) => r.run_id === item.run_id);
            return (
            <article className={`durable-entry ${item.kind === "hero_task" ? "durable-entry--hero" : ""}`} key={item.outbox_id}>
              <div className="durable-entry-meta"><strong>{labels[item.kind] || item.kind}</strong><span>{item.status === "sent" ? "Registrado" : item.status === "failed" ? "Falhou" : "Pendente"} · {time(item.accepted_at || item.created_at)}</span></div>
              {item.kind === "sms" && item.payload.purpose === "eta_request" && <small>Tentativa {deliveries.slice(0, i + 1).filter((d) => d.kind === "sms" && d.payload.purpose === "eta_request").length} de 10</small>}
              {typeof item.payload.title === "string" && <h3>{item.payload.title}</h3>}
              <p>{String(item.payload.text || item.payload.description || item.payload.body || "")}</p>
              {run && <details>
                <summary>Ver decisão do agente</summary>
                <p>{run.plan?.rationale}</p>
                <ul>{run.trajectory.map((step, j) => <li key={j}><code>{step.tool}</code> {step.accepted === false ? "· recusado" : ""}</li>)}</ul>
                <p>Modelo: {[...new Set(run.usage.map((u) => u.model))].join(", ") || "—"}</p>
                {run.trace_url?.startsWith("https://") && <a href={run.trace_url} target="_blank" rel="noreferrer">Abrir execução no LangSmith</a>}
              </details>}
            </article>
          ); })}
        </section>

        <aside className="durable-policy">
          <h2>SOP deste cenário</h2>
          <ol>
            <li>Risco alto: pedir a previsão de chegada imediatamente por SMS.</li>
            <li>Sem resposta: repetir a cada minuto, até completar dez SMS.</li>
            <li>Esperar um minuto após o décimo SMS.</li>
            <li>Abrir uma tarefa urgente para o hero ligar para o motorista e encerrar os pedidos.</li>
          </ol>
          <p>O intervalo começa na confirmação de cada SMS. O processamento da IA pode acrescentar alguns segundos.</p>
          <p>Fechar esta página não interrompe a carga. Reabra o mesmo link para acompanhar.</p>
          {loadId && <dl><dt>Carga</dt><dd>{loadId}</dd><dt>Execuções do agente</dt><dd>{snapshot?.runs.length || 0}</dd><dt>Timers disparados</dt><dd>{snapshot?.timers.filter((t) => t.status === "fired").length || 0}</dd><dt>Última atualização</dt><dd>{updated ? time(new Date(updated).toISOString()) : "—"}</dd></dl>}
          {state?.failed && <p className="banner banner--alert">Há uma falha registrada. Confira a decisão do agente e o histórico antes de iniciar outra execução.</p>}
        </aside>
      </div>
    </section>
  );
}
