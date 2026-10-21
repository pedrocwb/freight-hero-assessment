import clsx from "clsx";
import type { ReactNode } from "react";

import type { DelayRisk, RunStatus } from "../api";
import { useToasts } from "../state/toasts";

export function ScreenHead(props: { title: string; lede?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="screen-head">
      <div>
        <h1 className="screen-title">{props.title}</h1>
        {props.lede ? <p className="screen-lede">{props.lede}</p> : null}
      </div>
      {props.actions ? <div className="screen-actions">{props.actions}</div> : null}
    </div>
  );
}

export function Panel(props: {
  title: ReactNode;
  id?: string;
  actions?: ReactNode;
  className?: string;
  flush?: boolean;
  children: ReactNode;
}) {
  const headingId = props.id ? `${props.id}-title` : undefined;
  return (
    <section className={clsx("panel", props.className)} aria-labelledby={headingId}>
      <header className="panel-head">
        <h2 className="panel-title" id={headingId}>
          {props.title}
        </h2>
        {props.actions ? <div className="panel-actions">{props.actions}</div> : null}
      </header>
      <div className={clsx("panel-body", props.flush && "panel-body--flush")}>{props.children}</div>
    </section>
  );
}

/** An empty panel says what to do next. */
export function Empty(props: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty-title">{props.title}</p>
      {props.children ? <p className="empty-text">{props.children}</p> : null}
      {props.action ? <div className="empty-action">{props.action}</div> : null}
    </div>
  );
}

export function InlineError(props: { message: string; onRetry?: () => void }) {
  return (
    <div className="inline-error" role="alert">
      <span>{props.message}</span>
      {props.onRetry ? (
        <button type="button" className="btn btn--small" onClick={props.onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  acted: "Acted",
  noop: "No action",
  skipped: "Skipped",
  escalated: "Escalated",
  failed: "Failed",
};

export function StatusChip({ status }: { status: RunStatus }) {
  return <span className={clsx("chip", `chip--${status}`)}>{RUN_STATUS_LABEL[status] ?? status}</span>;
}

export function RiskChip({ risk }: { risk: DelayRisk }) {
  return (
    <span className={clsx("chip", `chip--risk-${risk}`)} title="Delay risk">
      {`${risk.charAt(0).toUpperCase()}${risk.slice(1)} risk`}
    </span>
  );
}

export function Tag({ children, tone }: { children: ReactNode; tone?: "sop" | "muted" | "urgent" }) {
  return <span className={clsx("tag", tone && `tag--${tone}`)}>{children}</span>;
}

export function Toasts() {
  const { toasts, dismiss } = useToasts();
  return (
    <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={clsx("toast", `toast--${toast.tone}`)} role={toast.tone === "error" ? "alert" : "status"}>
          <p className="toast-text">{toast.message}</p>
          <button type="button" className="toast-close" onClick={() => dismiss(toast.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function usd(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
  return `$${value.toFixed(digits)}`;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}
