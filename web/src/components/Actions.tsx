import clsx from "clsx";

import type { Action, OutboxItem } from "../api";
import { humanize, stageLabel } from "../lib/load";
import type { ActionGroup, DroppedAction } from "../lib/timeline";
import { Tag } from "./ui";

const text = (value: unknown): string => (typeof value === "string" ? value : value === undefined || value === null ? "" : String(value));

/** One action of a plan, as the agent proposed it. */
export function ActionLine({ action }: { action: Action }) {
  switch (action.type) {
    case "send_sms":
      return (
        <p className="action">
          <span className="action-kind">Text</span> <span className="mono">{action.contact_id}</span>
          <Tag tone="muted">{humanize(action.purpose)}</Tag>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "send_email":
      return (
        <p className="action">
          <span className="action-kind">Email</span> <span className="mono">{action.to_contact_ids.join(", ")}</span>
          <Tag tone="muted">{humanize(action.purpose)}</Tag>
          {action.subject ? <span className="action-text strong">{action.subject}</span> : null}
          <span className="action-text">{action.body}</span>
        </p>
      );
    case "post_broker_slack":
      return (
        <p className="action">
          <span className="action-kind">Broker Slack</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "write_tms_note":
      return (
        <p className="action">
          <span className="action-kind">TMS note</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "create_hero_task":
      return (
        <p className="action">
          <span className="action-kind">Hero task</span>
          {action.urgent ? <Tag tone="urgent">Urgent</Tag> : null}
          <span className="action-text strong">{action.title}</span>
          <span className="action-text">{action.description}</span>
        </p>
      );
    case "post_internal_slack":
      return (
        <p className="action">
          <span className="action-kind">Internal Slack</span>
          <span className="action-text">{action.text}</span>
        </p>
      );
    case "update_stage":
      return (
        <p className="action">
          <span className="action-kind">Stage</span> {stageLabel(action.stage)}
        </p>
      );
    case "schedule_timer":
      return (
        <p className="action">
          <span className="action-kind">Timer</span> follow up on the {humanize(action.kind)} in {action.due_in_minutes} min
        </p>
      );
    case "defer_obligation":
      return (
        <p className="action">
          <span className="action-kind">Defer</span> {humanize(action.kind)} by {action.minutes} min
        </p>
      );
    case "close_obligation":
      return (
        <p className="action">
          <span className="action-kind">Close</span> {humanize(action.kind)} as {action.status}
        </p>
      );
    case "open_obligation":
      return (
        <p className="action">
          <span className="action-kind">Open</span> {humanize(action.kind)}
        </p>
      );
    default: {
      const unknown = action as { type: string };
      return (
        <p className="action">
          <span className="action-kind">{humanize(unknown.type)}</span>
          <span className="action-text mono">{JSON.stringify(unknown)}</span>
        </p>
      );
    }
  }
}

const DELIVERY_CHIP: Record<OutboxItem["status"], string> = { sent: "chip--pass", pending: "chip--timer-pending", failed: "chip--fail" };

/** What one outbox item says, read from its payload (`application/executor.py`). */
function OutboxText({ item }: { item: OutboxItem }) {
  const p = item.payload;
  switch (item.kind) {
    case "sms":
      return (
        <>
          <span className="mono">to {text(p.contact_id)}</span>
          {p.purpose ? <Tag tone="muted">{humanize(text(p.purpose))}</Tag> : null}
          <span className="action-text">{text(p.text)}</span>
        </>
      );
    case "email": {
      const to = Array.isArray(p.to_contact_ids) ? p.to_contact_ids.map(text).join(", ") : "";
      return (
        <>
          <span className="mono">to {to}</span>
          {p.purpose ? <Tag tone="muted">{humanize(text(p.purpose))}</Tag> : null}
          {p.subject ? <span className="action-text strong">{text(p.subject)}</span> : null}
          <span className="action-text">{text(p.body)}</span>
        </>
      );
    }
    case "hero_task":
      return (
        <>
          {p.urgent ? <Tag tone="urgent">Urgent</Tag> : null}
          <span className="action-text strong">{text(p.title)}</span>
          {p.description ? <span className="action-text">{text(p.description)}</span> : null}
        </>
      );
    case "stage_update":
      return <span className="action-text">Stage updated to {stageLabel(text(p.stage))}</span>;
    default:
      return <span className="action-text">{text(p.text) || JSON.stringify(p)}</span>;
  }
}

/** The actions a run produced, by channel, with their delivery. */
export function ActionGroups({ groups }: { groups: readonly ActionGroup[] }) {
  return (
    <div className="action-groups">
      {groups.map((group) => (
        <section key={group.channel} className={clsx("action-group", `action-group--${group.channel}`)}>
          <h5 className="action-group-title">
            {group.label}
            {group.items.length > 1 ? <span className="muted"> ({group.items.length})</span> : null}
          </h5>
          <ul className="action-list">
            {group.items.map((item) => (
              <li key={item.outbox_id} className="action">
                <span className={clsx("chip", DELIVERY_CHIP[item.status])} title={item.last_error ?? undefined}>
                  {humanize(item.status)}
                </span>
                <OutboxText item={item} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** What the validator refused, and why. */
export function DroppedActions({ dropped }: { dropped: readonly DroppedAction[] }) {
  return (
    <ul className="plan">
      {dropped.map((d, i) => (
        <li key={i} className="plan-step plan-step--dropped">
          {d.action ? <ActionLine action={d.action} /> : null}
          {d.violations.map((v, j) => (
            <p key={j} className={clsx("violation", `violation--${v.severity}`)}>
              Dropped by <span className="mono">{v.rule}</span>
              {v.severity === "high" ? " (high severity)" : ""}: {v.message}
            </p>
          ))}
        </li>
      ))}
    </ul>
  );
}
