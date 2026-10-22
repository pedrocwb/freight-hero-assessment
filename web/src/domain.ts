/** Domain shapes returned by the durable broker workspace. */

export type Stage =
  | "dispatched"
  | "to_pickup"
  | "at_pickup"
  | "to_delivery"
  | "at_delivery"
  | "delivered";
export type DelayRisk = "low" | "medium" | "high";
export type Role = "driver" | "dispatcher" | "broker";
export type Channel = "sms" | "email" | "slack" | "system" | "hero";

/**
 * An event intent id. The catalog is data: each broker lists its intents
 * (the saved broker profile), so the page never hard-codes them.
 */
export type Intent = string;

export interface Contact {
  id: string;
  role: Role;
  channels: Channel[];
}

export interface Appointment {
  kind: "fixed" | "window";
  at: string | null;
  start: string | null;
  end: string | null;
}

export interface Stop {
  sequence: number;
  type: "pickup" | "delivery";
  name: string;
  address: string;
  timezone: string;
  appointment: Appointment;
}

export interface Load {
  load_id: string;
  broker_id: string;
  broker_name: string;
  shipper: string;
  carrier_name: string;
  mc_number: string;
  commodity: string;
  stage: Stage;
  delay_risk: DelayRisk;
  contacts: Contact[];
  stops: Stop[];
  instructions: string;
  load_types: string[];
  current_stop_sequence: number | null;
}

export type ObligationStatus = "open" | "satisfied" | "exhausted" | "cancelled";

export interface Obligation {
  obligation_id: string;
  load_id: string;
  /** An `ObligationDef.kind` of the load's broker: "eta_request", "pod", ... */
  kind: string;
  status: ObligationStatus;
  attempts: number;
  opened_at: string;
  last_action_at: string | null;
  deferred_until: string | null;
  deferrals_used: number;
  active_timer_id: string | null;
  sop_version: string | null;
}

export type TimerStatus = "pending" | "fired" | "superseded" | "cancelled";

export interface Timer {
  timer_id: string;
  load_id: string;
  obligation_id: string;
  due_at: string;
  created_at: string;
  status: TimerStatus;
  reason: string;
}

export type EventType =
  | "inbound_message"
  | "risk_changed"
  | "timer_due"
  | "attachment_received"
  | "hero_update"
  | "agent_action"
  | "document_processed";

export interface Attachment {
  attachment_id: string;
  filename: string;
  content_type: string;
  ref: string;
}

/** An entry of the load's event log (`domain.events.Event`). */
export interface LoadEvent {
  event_id: string;
  load_id: string;
  type: EventType;
  occurred_at: string;
  channel: Channel;
  direction: "inbound" | "outbound";
  sender_contact_id: string | null;
  body: string;
  attachments: Attachment[];
  data: Record<string, unknown>;
}

export type OutboxKind =
  | "sms"
  | "email"
  | "broker_slack"
  | "tms_note"
  | "hero_task"
  | "internal_slack"
  | "stage_update";

export interface OutboxItem {
  outbox_id: string;
  idempotency_key: string;
  run_id: string;
  load_id: string;
  kind: OutboxKind;
  payload: Record<string, unknown>;
  created_at: string;
  status: "pending" | "sent" | "failed";
  attempts: number;
  last_error: string | null;
}

export interface GateDecision {
  intent: Intent;
  act: boolean;
  reason: string;
}

export type Purpose = "eta_request" | "reply" | "escalation" | "info";

/**
 * `requests` names the obligation kind a message asks for ("eta_request", "pod"); the API
 * sets "eta_request" when `purpose` is `eta_request`. Records from before wave 3 omit it.
 */
export type Action =
  | { type: "send_sms"; contact_id: string; text: string; purpose: Purpose; requests?: string | null }
  | {
      type: "send_email";
      to_contact_ids: string[];
      body: string;
      subject: string | null;
      purpose: Purpose;
      requests?: string | null;
    }
  | { type: "post_broker_slack"; text: string }
  | { type: "write_tms_note"; text: string }
  | { type: "create_hero_task"; title: string; description: string; urgent: boolean }
  | { type: "post_internal_slack"; text: string }
  | { type: "update_stage"; stage: Stage }
  | { type: "schedule_timer"; kind: string; due_in_minutes: number }
  | { type: "defer_obligation"; kind: string; minutes: number }
  | {
      type: "close_obligation";
      kind: string;
      status: "satisfied" | "exhausted" | "cancelled";
    }
  | { type: "open_obligation"; kind: string };

export interface Plan {
  actions: Action[];
  rationale: string;
}

export interface Violation {
  rule: string;
  action_index: number;
  message: string;
  severity: "low" | "high";
}

export interface Usage {
  role: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  latency_ms: number;
}

export type RunStatus = "skipped" | "noop" | "acted" | "escalated" | "failed";

/**
 * One tool call of the agent loop (`domain.runs.TrajectoryStep`, card AGT). `accepted` is
 * null for a read tool, true or false for an action tool the rules checked at the call.
 */
export interface TrajectoryStep {
  tool: string;
  args: Record<string, unknown>;
  result: string;
  accepted: boolean | null;
  cost_usd: number;
}

export interface RunRecord {
  run_id: string;
  event_id: string;
  load_id: string;
  started_at: string;
  finished_at: string | null;
  status: RunStatus;
  gate: GateDecision | null;
  sop_section_ids: string[];
  plan: Plan | null;
  violations: Violation[];
  usage: Usage[];
  skip_reason: string | null;
  error: string | null;
  /** The run's LangSmith trace when tracing is on. */
  trace_url: string | null;
  sop_version?: string | null;
  intent: Intent | null;
  broker_id: string | null;
  trajectory: TrajectoryStep[];
  sections_searched: string[];
  rejections: number;
  /** The graph nodes the run went through, in order, one entry per visit (LangGraph node ids). */
  nodes: string[];
  /** The facility notes the agent's prompt showed: the load's current and next stop. */
  facility_facts?: FacilityFact[];
  /** What read the message at the gate: "llm" or "jev"; null when code decided. */
  gate_engine?: string | null;
  /** The jev engine's probabilities: each intent's, and P(act now). */
  gate_scores?: GateScores | null;
}

export interface GateScores {
  model: string;
  intents: Record<string, number>;
  act: number;
  threshold: number;
  intent_confidence?: number | null;
}

// --- facility memory shown in agent run context --------------------------------------

export type FactKind = "check_in" | "hours" | "requirement" | "access" | "contact" | "other";

/** One note an earlier load of the same client left about a facility. Untrusted text. */
export interface FacilityFact {
  fact_id: string;
  client_id: string;
  facility_key: string;
  facility_name: string;
  text: string;
  kind: FactKind;
  source_load_id: string;
  source_event_id: string;
  source_at: string;
  created_at: string;
  updated_at: string;
}
