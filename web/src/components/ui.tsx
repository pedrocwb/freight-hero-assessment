import clsx from "clsx";
import type { ReactNode } from "react";

import type { DelayRisk, HealthLevel, RunStatus } from "../api";
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

/** Shown on screens that need a selected load when there is none. */
export function NoLoad(props: { what: string }) {
  return (
    <Empty title="No load selected">
      Create a load from a fixture in the Control room to start; {props.what} shows here once it
      exists.
    </Empty>
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

const LEVEL_LABEL: Record<HealthLevel, string> = { ok: "Healthy", warn: "Warning", critical: "Critical" };

export function LevelChip({ level }: { level: HealthLevel }) {
  return <span className={clsx("chip", `chip--level-${level}`)}>{LEVEL_LABEL[level] ?? level}</span>;
}

export function Tag({ children, tone }: { children: ReactNode; tone?: "sop" | "muted" | "urgent" | "agent" }) {
  return <span className={clsx("tag", tone && `tag--${tone}`)}>{children}</span>;
}

/** A horizontal meter: value against a limit, with the limit marked when it is in range. */
export function Meter(props: {
  value: number;
  max: number;
  label: string;
  warnAt?: number;
  valueText: string;
}) {
  const ratio = props.max > 0 ? props.value / props.max : 0;
  const level = ratio > 1 ? "critical" : props.warnAt !== undefined && ratio > props.warnAt ? "warn" : "ok";
  return (
    <div className="meter">
      <div className="meter-row">
        <span className="meter-label">{props.label}</span>
        <span className="meter-value">{props.valueText}</span>
      </div>
      <div
        className="meter-track"
        role="meter"
        aria-label={props.label}
        aria-valuemin={0}
        aria-valuemax={props.max}
        aria-valuenow={Math.round(props.value * 1000) / 1000}
        aria-valuetext={props.valueText}
      >
        <div className={clsx("meter-fill", `meter-fill--${level}`)} style={{ width: `${Math.min(ratio, 1) * 100}%` }} />
      </div>
    </div>
  );
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
