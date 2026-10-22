import { useEffect, useRef, useState, type FormEvent } from "react";

import { conversation, durableRequest, progress, remaining, type DemoPolicy, type DurableHealth, type DurableSnapshot } from "../lib/durable";
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
  const [policy, setPolicy] = useState<DemoPolicy | null>(null);
  const [policyError, setPolicyError] = useState("");
  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void durableRequest<DemoPolicy>("/sop", { signal: controller.signal }).then((defaults) => {
      if (controller.signal.aborted) return;
      try {
        const saved = JSON.parse(localStorage.getItem("aurora-sop-draft") || "null");
        setPolicy(saved && typeof saved.body === "string" && Number.isInteger(saved.max_attempts) && Number.isInteger(saved.interval_minutes) ? saved : defaults);
      } catch { setPolicy(defaults); }
    }).catch(() => { if (!controller.signal.aborted) setPolicyError("Não foi possível carregar o SOP. Recarregue a página."); });
    return () => controller.abort();
  }, []);

  function editPolicy(change: Partial<DemoPolicy>) {
    if (!policy) return;
    const next = { ...policy, ...change };
    setPolicy(next);
    localStorage.setItem("aurora-sop-draft", JSON.stringify(next));
  }

  async function stopExecution() {
    setStopBusy(true);
    setStopError("");
    try {
      await durableRequest(`/loads/${encodeURIComponent(loadId)}/stop`, { method: "POST" });
      setSnapshot(await durableRequest<DurableSnapshot>(`/loads/${encodeURIComponent(loadId)}`));
    } catch (cause) {
      setStopError(cause instanceof Error ? cause.message : "Não foi possível parar. Tente novamente.");
    } finally { setStopBusy(false); }
  }

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
    if (!policy) return;
    setStarting(true);
    setError("");
    // Retain the ID on an uncertain response: retrying cannot start another load.
    let saved: { load_id: string; policy: DemoPolicy } | null = null;
    try { saved = JSON.parse(sessionStorage.getItem("aurora-start-request") || "null"); } catch { /* replace invalid draft */ }
    const pending = saved && JSON.stringify(saved.policy) === JSON.stringify(policy) ? saved : { load_id: `aurora-${crypto.randomUUID()}`, policy };
    sessionStorage.setItem("aurora-start-request", JSON.stringify(pending));
    try {
      const result = await durableRequest<{ load_id: string }>(`/demos/aurora/${pending.load_id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending.policy) });
      sessionStorage.removeItem("aurora-start-request");
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
  const halted = !!snapshot?.load.automation_stopped_at;
  const stopping = !!snapshot?.load.automation_stop_requested_at && !halted;
  const maxAttempts = state?.maxAttempts ?? policy?.max_attempts ?? 10;
  const intervalMinutes = state?.intervalMinutes ?? policy?.interval_minutes ?? 1;
  const validPolicy = policy && policy.body.trim().length >= 20 && policy.body.length <= 12000 && Number.isInteger(policy.max_attempts) && policy.max_attempts >= 1 && policy.max_attempts <= 10 && Number.isInteger(policy.interval_minutes) && policy.interval_minutes >= 1 && policy.interval_minutes <= 60;
  const stopped = state?.obligation && ["satisfied", "cancelled"].includes(state.obligation.status);
  const status = error ? "Conexão interrompida" : halted ? "Execução parada" : stopping ? "Parando execução…" : state?.failed ? "A execução precisa de atenção" : state?.complete ? "Escalado para um hero" : stopped ? "Acompanhamento encerrado" : state?.timer ? "Aguardando o motorista" : loadId ? "Agente processando" : "Pronto para iniciar";
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
        <div className="durable-controls">
          {loadId && <button className="button" onClick={() => void stopExecution()} disabled={!snapshot || stopBusy || stopping || halted || !!error}>{stopping || stopBusy ? "Parando…" : "Parar execução"}</button>}
          <button className="button button--primary" onClick={() => void start()} disabled={starting || !validPolicy || !health?.model_ready || !!error}>
            {starting ? "Iniciando…" : loadId ? "Executar novo SOP" : "Iniciar cenário"}
          </button>
        </div>
      </div>

      <p className="durable-mode">IA ao vivo · Relógio real · SMS e tarefas registrados em ambiente de teste</p>
      {stopError && <p className="banner banner--alert" role="alert">{stopError}</p>}
      {policyError && <p className="banner banner--alert" role="alert">{policyError}</p>}
      <details className="durable-editor">
        <summary>Editar SOP para nova execução</summary>
        <p>Este rascunho vale para uma nova carga. A execução atual mantém o SOP com que começou.</p>
        {policy && <fieldset disabled={starting}>
          <div className="durable-policy-fields">
            <label>Tentativas de SMS<input type="number" min={1} max={10} value={policy.max_attempts} onChange={(e) => editPolicy({ max_attempts: Number(e.target.value) })} /></label>
            <label>Intervalo em minutos<input type="number" min={1} max={60} value={policy.interval_minutes} onChange={(e) => editPolicy({ interval_minutes: Number(e.target.value) })} /></label>
          </div>
          <label htmlFor="sop-body">SOP de risco alto e acompanhamento</label>
          <textarea id="sop-body" rows={12} maxLength={12000} value={policy.body} onChange={(e) => editPolicy({ body: e.target.value })} />
          <small>Use {"{{max_attempts}}"} e {"{{interval_minutes}}"} no texto para acompanhar os valores acima. Limites: 1–10 tentativas e 1–60 minutos. As outras seções do SOP são preservadas.</small>
          {!validPolicy && <p role="alert">Confira os limites e escreva pelo menos 20 caracteres no SOP.</p>}
        </fieldset>}
      </details>
      {error && <p className="banner banner--alert" role="alert">{error} Os dados abaixo são da última atualização; a tela tenta reconectar automaticamente.</p>}
      {health && !health.model_ready && <p className="banner banner--warn">A IA ainda não está configurada no serviço da operação ao vivo.</p>}
      {!loadId && lastLoad && <a href={href("durable", { load: lastLoad })}>Retomar a última execução</a>}

      <div className="durable-status" role="status">
        <div><span className="durable-status-dot" /><strong>{status}</strong>
          <p>{halted ? "Parada confirmada. Timers e ações pendentes foram cancelados; o histórico foi preservado." : stopping ? "Aguardando a etapa em andamento terminar. Nenhum novo contato será iniciado após a confirmação da parada." : state?.complete ? "O limite de tentativas foi atingido. O hero deve ligar para confirmar a chegada." : stopped ? "A obrigação foi encerrada e os próximos pedidos foram cancelados." : driverReplied ? "O motorista respondeu. O agente avalia a mensagem conforme o SOP." : "Risco alto de atraso na entrega. O motorista não responde aos pedidos de previsão."}</p>
        </div>
        <div className="durable-countdown">
          <span>{state?.timer ? (state.sms.length === maxAttempts ? "Escalada em" : "Próximo contato em") : "Contatos confirmados"}</span>
          <strong>{state?.timer && !error ? remaining(state.timer.due_at, now) : `${state?.sms.length || 0}/${maxAttempts}`}</strong>
          {state?.timer && <small>{time(state.timer.due_at)} · horário de Brasília{Date.parse(state.timer.due_at) <= now ? " · aguardando processamento" : ""}</small>}
        </div>
      </div>

      <ol className="durable-attempts" aria-label="Tentativas de contato">
        {Array.from({ length: maxAttempts }, (_, i) => {
          const sms = state?.sms[i];
          return <li key={i} className={sms ? "is-sent" : ""}>
            <span>SMS {i + 1}</span><strong>{sms ? "Registrado" : halted || stopped ? "Cancelado" : "Aguardando"}</strong>
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
              onChange={(event) => setMessage(event.target.value)} disabled={!snapshot || sending || stopping || halted}
              aria-describedby="driver-message-hint" />
            <div><small id="driver-message-hint">Simula um SMS recebido nesta carga. Uma previsão de chegada pode encerrar os pedidos.</small>
              <button className="button button--primary" type="submit" disabled={!snapshot || !message.trim() || sending || !!error || stopping || halted}>{sending ? "Enviando…" : "Enviar como motorista"}</button>
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
              <div className="durable-entry-meta"><strong>{labels[item.kind] || item.kind}</strong><span>{item.status === "sent" ? "Registrado" : item.status === "failed" ? "Falhou" : item.status === "cancelled" ? "Cancelado" : "Pendente"} · {time(item.accepted_at || item.created_at)}</span></div>
              {item.kind === "sms" && item.payload.purpose === "eta_request" && <small>Tentativa {deliveries.slice(0, i + 1).filter((d) => d.kind === "sms" && d.payload.purpose === "eta_request").length} de {maxAttempts}</small>}
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
          <h2>SOP desta execução</h2>
          <p>Até <strong>{maxAttempts} SMS</strong>, com espera de <strong>{intervalMinutes} minuto(s)</strong> após cada confirmação. O texto do SOP define as decisões e a escalada.</p>
          <p>O intervalo começa na confirmação de cada SMS. O processamento da IA pode acrescentar alguns segundos.</p>
          <p>Fechar esta página não interrompe a carga. Reabra o mesmo link para acompanhar.</p>
          {snapshot?.load.sop_snapshot && <details><summary>SOP usado nesta execução · {snapshot.load.sop_snapshot.version}</summary><pre className="durable-sop-text">{snapshot.load.sop_snapshot.policy.body.replaceAll("{{max_attempts}}", String(maxAttempts)).replaceAll("{{interval_minutes}}", String(intervalMinutes))}</pre></details>}
          {loadId && <dl><dt>Carga</dt><dd>{loadId}</dd><dt>Execuções do agente</dt><dd>{snapshot?.runs.length || 0}</dd><dt>Timers disparados</dt><dd>{snapshot?.timers.filter((t) => t.status === "fired").length || 0}</dd><dt>Última atualização</dt><dd>{updated ? time(new Date(updated).toISOString()) : "—"}</dd></dl>}
          {state?.failed && <p className="banner banner--alert">Há uma falha registrada. Confira a decisão do agente e o histórico antes de iniciar outra execução.</p>}
        </aside>
      </div>
    </section>
  );
}
