/**
 * Seed data for mock mode: contract-shaped records of the assignment's story (load 481207,
 * Northline, A1 to A4) and of scenario D1 on a Keystone load, as the API would return them
 * at 08:02 on 2026-10-13. Texts follow the eval scenarios' scripts.
 */

import type {
  Action,
  Load,
  LoadEvent,
  LoadMetadata,
  Obligation,
  OutboxItem,
  OutboxKind,
  RunRecord,
  Timer,
  Usage,
} from "../api";

export const TZ = "America/Los_Angeles";
export const DAY = "2026-10-13";
export const SEED_NOW = at("08:02");

/** A local time on the story's day, as the API serializes it (PDT, -07:00). */
export function at(hhmm: string, seconds = 0): string {
  return `${DAY}T${hhmm}:${String(seconds).padStart(2, "0")}-07:00`;
}

export const NORTHLINE_METADATA: LoadMetadata = {
  load_id: "481207",
  broker: { id: "northline", name: "Northline Logistics" },
  shipper: "Riverbend Foods",
  carrier: { name: "Prairie Haul LLC", mc_number: "778120" },
  commodity: "Canned tomato sauce, 38,000 lb, dry van",
  stage: "to_pickup",
  delay_risk: "low",
  contacts: [
    { id: "drv_20931", role: "driver", channels: ["sms"] },
    { id: "dsp_4410", role: "dispatcher", channels: ["email"] },
    { id: "brk_0087", role: "broker", channels: ["email", "slack"] },
  ],
  stops: [
    {
      sequence: 1,
      type: "pickup",
      name: "Riverbend plant",
      address: "Stockton, CA",
      timezone: TZ,
      appointment: { type: "window", start: "2026-10-13T06:00", end: "2026-10-13T14:00" },
    },
    {
      sequence: 2,
      type: "delivery",
      name: "Valley DC",
      address: "Reno, NV",
      timezone: TZ,
      appointment: { type: "fixed", time: "2026-10-14T09:30" },
    },
  ],
  instructions: "Call 1 hr before arrival. PO 55821 required at gate.",
};

export const KEYSTONE_METADATA: LoadMetadata = {
  ...NORTHLINE_METADATA,
  load_id: "590114",
  broker: { id: "keystone", name: "Keystone Freight" },
  commodity: "Frozen vegetables, 40,000 lb, reefer at -10 F",
  load_types: ["reefer"],
};

export const FIXTURES = [
  { name: "load_481207", metadata: NORTHLINE_METADATA },
  {
    name: "load_481207_keystone",
    metadata: { ...NORTHLINE_METADATA, broker: { id: "keystone", name: "Keystone Freight" } },
  },
  { name: "load_481207_keystone_reefer", metadata: { ...KEYSTONE_METADATA, load_id: "481207" } },
];

// --- metadata to the Load shape, like domain.load.Load.from_metadata --------------------

function offsetMinutes(utcMs: number, timeZone: string): number {
  const name =
    new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "longOffset" })
      .formatToParts(new Date(utcMs))
      .find((p) => p.type === "timeZoneName")?.value ?? "GMT";
  const m = /GMT([+-])(\d{2}):?(\d{2})?/.exec(name);
  if (!m) return 0;
  const sign = m[1] === "-" ? -1 : 1;
  return sign * (Number(m[2]) * 60 + Number(m[3] ?? 0));
}

/** "2026-10-13T06:00" in a zone, as an ISO string with that zone's offset. */
export function zoned(local: string, timeZone: string): string {
  const [date, time = "00:00"] = local.split("T");
  const [y, mo, d] = (date ?? "").split("-").map(Number);
  const [h, mi] = time.split(":").map(Number);
  const guess = Date.UTC(y ?? 1970, (mo ?? 1) - 1, d ?? 1, h ?? 0, mi ?? 0);
  const offset = offsetMinutes(guess - offsetMinutes(guess, timeZone) * 60000, timeZone);
  const sign = offset < 0 ? "-" : "+";
  const abs = Math.abs(offset);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  return `${date}T${String(h ?? 0).padStart(2, "0")}:${String(mi ?? 0).padStart(2, "0")}:00${sign}${hh}:${mm}`;
}

type Raw = Record<string, unknown>;

export function loadFromMetadata(meta: LoadMetadata): Load {
  const m = meta as Raw;
  const broker = m.broker as Raw | undefined;
  const carrier = m.carrier as Raw | undefined;
  const required = ["load_id", "shipper", "commodity", "stage", "delay_risk", "contacts", "stops"];
  const missing = required.filter((k) => m[k] === undefined);
  if (!broker?.id || !broker?.name) missing.push("broker");
  if (!carrier?.name) missing.push("carrier");
  if (missing.length) throw new Error(`invalid load metadata: missing ${missing.join(", ")}`);
  return {
    load_id: String(m.load_id),
    broker_id: String(broker!.id),
    broker_name: String(broker!.name),
    shipper: String(m.shipper),
    carrier_name: String(carrier!.name),
    mc_number: String(carrier!.mc_number ?? ""),
    commodity: String(m.commodity),
    stage: m.stage as Load["stage"],
    delay_risk: m.delay_risk as Load["delay_risk"],
    contacts: (m.contacts as Load["contacts"]).map((c) => ({ id: c.id, role: c.role, channels: [...c.channels] })),
    stops: (m.stops as Raw[]).map((s) => {
      const tz = String(s.timezone);
      const appt = s.appointment as Raw;
      const fixed = appt.type === "fixed";
      return {
        sequence: Number(s.sequence),
        type: s.type as "pickup" | "delivery",
        name: String(s.name),
        address: String(s.address),
        timezone: tz,
        appointment: {
          kind: fixed ? "fixed" : "window",
          at: fixed ? zoned(String(appt.time), tz) : null,
          start: fixed ? null : zoned(String(appt.start), tz),
          end: fixed ? null : zoned(String(appt.end), tz),
        },
      };
    }),
    instructions: String(m.instructions ?? ""),
    load_types: Array.isArray(m.load_types) ? (m.load_types as string[]) : [],
    current_stop_sequence: null,
  };
}

// --- the story -------------------------------------------------------------------------

export interface MockLoad {
  load: Load;
  obligations: Obligation[];
  timers: Timer[];
  events: LoadEvent[];
  outbox: OutboxItem[];
  runs: RunRecord[];
}

const GATE_MODEL = "openai/gpt-4.1-nano";
const DECIDE_MODEL = "google/gemini-2.5-flash-lite";

function usage(role: "gate" | "decide", input: number, output: number, latency: number): Usage {
  const price = role === "gate" ? { in: 0.1, out: 0.4 } : { in: 0.1, out: 0.4 };
  return {
    role,
    model: role === "gate" ? GATE_MODEL : DECIDE_MODEL,
    input_tokens: input,
    output_tokens: output,
    cached_tokens: role === "decide" ? Math.round(input * 0.4) : 0,
    cost_usd: (input * price.in + output * price.out) / 1_000_000,
    latency_ms: latency,
  };
}

const CHANNEL_OF: Record<OutboxKind, LoadEvent["channel"]> = {
  sms: "sms",
  email: "email",
  broker_slack: "slack",
  hero_task: "hero",
  internal_slack: "hero",
  tms_note: "system",
  stage_update: "system",
};

function outboxText(kind: OutboxKind, payload: Raw): string {
  if (kind === "email") return String(payload.body ?? "");
  if (kind === "hero_task") return `${String(payload.title)}\n${String(payload.description)}`;
  if (kind === "stage_update") return String(payload.stage ?? "");
  return String(payload.text ?? "");
}

/** The outbox items and agent_action events for a run's approved actions. */
function effects(
  loadId: string,
  brokerId: string,
  run: RunRecord,
  approved: Action[],
  firstOutbox: number,
  now: string,
): { outbox: OutboxItem[]; events: LoadEvent[] } {
  const outbox: OutboxItem[] = [];
  const events: LoadEvent[] = [];
  approved.forEach((action, index) => {
    let kind: OutboxKind | null = null;
    let payload: Raw = {};
    switch (action.type) {
      case "send_sms":
        kind = "sms";
        payload = { contact_id: action.contact_id, text: action.text, purpose: action.purpose };
        break;
      case "send_email":
        kind = "email";
        payload = { to_contact_ids: action.to_contact_ids, subject: action.subject, body: action.body, purpose: action.purpose };
        break;
      case "post_broker_slack":
        kind = "broker_slack";
        payload = { broker_id: brokerId, text: action.text };
        break;
      case "write_tms_note":
        kind = "tms_note";
        payload = { text: action.text };
        break;
      case "create_hero_task":
        kind = "hero_task";
        payload = { title: action.title, description: action.description, urgent: action.urgent };
        break;
      default:
        break;
    }
    if (!kind) return;
    const key = `${run.event_id}:${index}`;
    const outboxId = `out_${loadId}_${firstOutbox + outbox.length}`;
    outbox.push({
      outbox_id: outboxId,
      idempotency_key: key,
      run_id: run.run_id,
      load_id: loadId,
      kind,
      payload,
      created_at: now,
      status: "sent",
      attempts: 1,
      last_error: null,
    });
    events.push({
      event_id: `act:${key}`,
      load_id: loadId,
      type: "agent_action",
      occurred_at: now,
      channel: CHANNEL_OF[kind],
      direction: "outbound",
      sender_contact_id: null,
      body: outboxText(kind, payload),
      attachments: [],
      data: { kind, payload, run_id: run.run_id, outbox_id: outboxId },
    });
  });
  return { outbox, events };
}

function inbound(loadId: string, id: string, time: string, channel: "sms" | "email" | "slack", sender: string, body: string): LoadEvent {
  return {
    event_id: id,
    load_id: loadId,
    type: "inbound_message",
    occurred_at: time,
    channel,
    direction: "inbound",
    sender_contact_id: sender,
    body,
    attachments: [],
    data: {},
  };
}

function riskEvent(loadId: string, pingId: string, time: string, previous: string, current: string): LoadEvent {
  return {
    event_id: `risk:${pingId}`,
    load_id: loadId,
    type: "risk_changed",
    occurred_at: time,
    channel: "system",
    direction: "inbound",
    sender_contact_id: null,
    body: "",
    attachments: [],
    data: { previous, current, ping_id: pingId },
  };
}

function timerEvent(loadId: string, timer: Timer, firedAt: string): LoadEvent {
  return {
    event_id: `timer:${timer.timer_id}`,
    load_id: loadId,
    type: "timer_due",
    occurred_at: firedAt,
    channel: "system",
    direction: "inbound",
    sender_contact_id: null,
    body: "",
    attachments: [],
    data: { timer_id: timer.timer_id, obligation_id: timer.obligation_id, kind: "eta_request", due_at: timer.due_at },
  };
}

interface RunSpec {
  run: Omit<RunRecord, "load_id" | "finished_at" | "trace_url" | "skip_reason" | "error" | "violations"> &
    Partial<Pick<RunRecord, "violations" | "skip_reason" | "error">>;
  seconds: number;
  dropped?: number[];
}

function assemble(loadId: string, brokerId: string, triggers: LoadEvent[], specs: RunSpec[]) {
  const runs: RunRecord[] = [];
  const outbox: OutboxItem[] = [];
  const events: LoadEvent[] = [];
  specs.forEach((spec, i) => {
    const trigger = triggers[i]!;
    events.push(trigger);
    const start = Date.parse(spec.run.started_at);
    const run: RunRecord = {
      violations: [],
      skip_reason: null,
      error: null,
      ...spec.run,
      load_id: loadId,
      finished_at: new Date(start + spec.seconds * 1000).toISOString(),
      trace_url: null,
    };
    runs.push(run);
    const approved = (run.plan?.actions ?? []).filter((_, index) => !(spec.dropped ?? []).includes(index));
    const made = effects(loadId, brokerId, run, approved, outbox.length + 1, run.started_at);
    outbox.push(...made.outbox);
    events.push(...made.events);
  });
  return { runs, outbox, events };
}

function northline(): MockLoad {
  const id = "481207";
  const load = { ...loadFromMetadata(NORTHLINE_METADATA), delay_risk: "high" as const };
  const t1: Timer = { timer_id: "timer_1", load_id: id, obligation_id: "obl_1", due_at: at("07:31"), created_at: at("07:01"), status: "fired", reason: "eta_request follow-up" };
  const t2: Timer = { timer_id: "timer_2", load_id: id, obligation_id: "obl_1", due_at: at("08:01"), created_at: at("07:31"), status: "fired", reason: "eta_request follow-up" };
  const triggers = [
    riskEvent(id, "ping_a1", at("07:00"), "low", "high"),
    inbound(id, "msg_a2", at("07:12"), "sms", "drv_20931", "What's the PO number?"),
    timerEvent(id, t1, at("07:31")),
    timerEvent(id, t2, at("08:01")),
  ];
  const story = assemble(id, load.broker_id, triggers, [
    {
      seconds: 3.4,
      run: {
        run_id: "run_1",
        event_id: "risk:ping_a1",
        started_at: at("07:01"),
        status: "acted",
        gate: { intent: "delay_risk_high", act: true, reason: "Delay risk changed from low to high (intent set by code)." },
        sop_section_ids: ["communication_rules", "delay_risk_high", "escalation"],
        plan: {
          rationale: "delay_risk_high (Northline), first step: text the driver for the ETA and follow up in 30 minutes.",
          actions: [
            { type: "send_sms", contact_id: "drv_20931", purpose: "eta_request", text: "Hi, this is Northline Logistics. What time will you arrive at the Riverbend plant pickup in Stockton?" },
            { type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 },
          ],
        },
        usage: [usage("decide", 2860, 142, 1850)],
      },
    },
    {
      seconds: 4.1,
      run: {
        run_id: "run_2",
        event_id: "msg_a2",
        started_at: at("07:13"),
        status: "acted",
        gate: { intent: "question", act: true, reason: "The driver asks for the PO number; the message has no ETA." },
        sop_section_ids: ["communication_rules", "driver_questions", "escalation"],
        plan: {
          rationale: "driver_questions: the PO is in the load instructions. A reply, not an ETA request; the open request and its 07:31 follow-up stay as they are.",
          actions: [{ type: "send_sms", contact_id: "drv_20931", purpose: "reply", text: "The PO number is 55821. It is required at the gate." }],
        },
        usage: [usage("gate", 1210, 38, 640), usage("decide", 3120, 120, 1720)],
      },
    },
    {
      seconds: 3.2,
      run: {
        run_id: "run_3",
        event_id: "timer:timer_1",
        started_at: at("07:31"),
        status: "acted",
        gate: { intent: "eta_follow_up_due", act: true, reason: "Follow-up timer of the open ETA request (intent set by code)." },
        sop_section_ids: ["communication_rules", "delay_risk_high", "escalation"],
        plan: {
          rationale: "One ETA request so far and no ETA: send the second request and follow up in 30 minutes.",
          actions: [
            { type: "send_sms", contact_id: "drv_20931", purpose: "eta_request", text: "Following up: what time will you arrive at the Riverbend plant in Stockton?" },
            { type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 },
          ],
        },
        usage: [usage("decide", 3340, 131, 1690)],
      },
    },
    {
      seconds: 4.8,
      dropped: [2],
      run: {
        run_id: "run_4",
        event_id: "timer:timer_2",
        started_at: at("08:01"),
        status: "acted",
        gate: { intent: "eta_follow_up_due", act: true, reason: "Follow-up timer of the open ETA request (intent set by code)." },
        sop_section_ids: ["communication_rules", "delay_risk_high", "escalation"],
        plan: {
          rationale: "Two ETA requests with no answer: email the dispatcher in the main thread, then an urgent Hero task to call the driver, and close the request as exhausted.",
          actions: [
            { type: "send_email", to_contact_ids: ["dsp_4410"], subject: "Load 481207: ETA needed for the Riverbend pickup", purpose: "escalation", body: "Your driver does not answer. Please send the ETA for the pickup at the Riverbend plant in Stockton." },
            { type: "create_hero_task", urgent: true, title: "Call the driver for the pickup ETA", description: "Load 481207, pickup at Riverbend plant, Stockton. Delay risk went high at 07:00. ETA asked by SMS at 07:01 and 07:31, no answer; the dispatcher was emailed at 08:01. Call drv_20931 for the ETA and note it on the task." },
            { type: "send_sms", contact_id: "drv_20931", purpose: "eta_request", text: "Please send your ETA to the Riverbend plant." },
            { type: "close_obligation", kind: "eta_request", status: "exhausted" },
          ],
        },
        violations: [{ rule: "eta_attempt_cap", action_index: 2, severity: "low", message: "2 of 2 ETA requests already sent for this obligation." }],
        usage: [usage("decide", 3610, 236, 2210)],
      },
    },
  ]);
  return {
    load,
    obligations: [
      { obligation_id: "obl_1", load_id: id, kind: "eta_request", status: "exhausted", attempts: 2, opened_at: at("07:01"), last_action_at: at("07:31"), deferred_until: null, deferrals_used: 0, active_timer_id: null, sop_version: "c41f9a7e2b13" },
    ],
    timers: [t1, t2],
    ...story,
  };
}

function keystone(): MockLoad {
  const id = "590114";
  const load = { ...loadFromMetadata(KEYSTONE_METADATA), delay_risk: "high" as const };
  const t1: Timer = { timer_id: "timer_k1", load_id: id, obligation_id: "obl_k1", due_at: at("07:46"), created_at: at("07:16"), status: "fired", reason: "eta_request follow-up" };
  const triggers = [
    riskEvent(id, "ping_d1", at("07:15"), "low", "high"),
    timerEvent(id, t1, at("07:46")),
    inbound(id, "msg_d1_broker", at("07:52"), "slack", "brk_0087", "Thanks. I'll call the carrier."),
  ];
  const story = assemble(id, load.broker_id, triggers, [
    {
      seconds: 3.0,
      run: {
        run_id: "run_k1",
        event_id: "risk:ping_d1",
        started_at: at("07:16"),
        status: "acted",
        gate: { intent: "delay_risk_high", act: true, reason: "Delay risk changed from low to high (intent set by code)." },
        sop_section_ids: ["communication_rules", "delay_risk_high", "escalation"],
        plan: {
          rationale: "Keystone: one SMS to the driver for the ETA, then wait 30 minutes.",
          actions: [
            { type: "send_sms", contact_id: "drv_20931", purpose: "eta_request", text: "Hi, this is Keystone Freight. What time will you arrive at the Riverbend plant pickup in Stockton?" },
            { type: "schedule_timer", kind: "eta_request", due_in_minutes: 30 },
          ],
        },
        usage: [usage("decide", 2790, 118, 1640)],
      },
    },
    {
      seconds: 3.6,
      run: {
        run_id: "run_k2",
        event_id: "timer:timer_k1",
        started_at: at("07:46"),
        status: "acted",
        gate: { intent: "eta_follow_up_due", act: true, reason: "Follow-up timer of the open ETA request (intent set by code)." },
        sop_section_ids: ["communication_rules", "delay_risk_high", "escalation"],
        plan: {
          rationale: "Keystone: no ETA 30 minutes after the one SMS, so alert the broker's Slack channel. Never contact the dispatcher.",
          actions: [
            { type: "post_broker_slack", text: "Load 590114: delay risk is high for the Riverbend plant pickup in Stockton and the driver has not sent an ETA since our text at 07:16." },
            { type: "close_obligation", kind: "eta_request", status: "exhausted" },
          ],
        },
        usage: [usage("decide", 2950, 104, 1580)],
      },
    },
    {
      seconds: 1.1,
      run: {
        run_id: "run_k3",
        event_id: "msg_d1_broker",
        started_at: at("07:53"),
        status: "noop",
        gate: { intent: "acknowledgement", act: false, reason: "The broker thanks us and takes it from here; nothing is asked." },
        sop_section_ids: [],
        plan: null,
        usage: [usage("gate", 1180, 31, 590)],
      },
    },
  ]);
  return {
    load,
    obligations: [
      { obligation_id: "obl_k1", load_id: id, kind: "eta_request", status: "exhausted", attempts: 1, opened_at: at("07:16"), last_action_at: at("07:16"), deferred_until: null, deferrals_used: 0, active_timer_id: null, sop_version: "c41f9a7e2b13" },
    ],
    timers: [t1],
    ...story,
  };
}

export function seedLoads(): MockLoad[] {
  return [northline(), keystone()];
}

export const SCENARIOS = [
  ["a_client_a_high_risk", "A: Client A, delay risk goes high, the driver asks for the PO and never gives an ETA", ["regression", "sop:delay_risk_high", "sop:driver_questions", "sop:escalation", "sop:communication_rules"]],
  ["a_question_during_last_wait", "A variant: the driver asks for the PO after the second ETA request", ["regression", "sop:delay_risk_high", "sop:driver_questions", "sop:escalation"]],
  ["b_ack_after_eta_request", "B2: the driver says ok to the ETA request, so nothing changes and the follow-up still runs", ["regression", "sop:acknowledgements", "sop:delay_risk_high"]],
  ["b_ack_with_nothing_open", "B1: the driver says ok and nothing is open, so the agent does nothing", ["regression", "sop:acknowledgements"]],
  ["b_broker_thanks_by_email", "B5: the broker says thanks by email, so the agent does nothing", ["regression", "sop:acknowledgements"]],
  ["b_eta_received_cancels_follow_up", "B3: the ETA arrives at 07:20, so the 07:31 follow-up never runs", ["regression", "sop:eta_received", "sop:delay_risk_high"]],
  ["b_medium_risk_does_not_wake", "B4: pings at low and medium do not wake the agent; only high does", ["regression", "sop:delay_risk_high"]],
  ["c1_eta_from_driver", "The driver sends an ETA after the first SMS; the request closes as satisfied", ["regression", "sop:delay_risk_high", "sop:eta_received"]],
  ["c2_eta_from_dispatcher", "The dispatcher emails the ETA; the request closes as satisfied", ["regression", "sop:delay_risk_high", "sop:eta_received"]],
  ["c3_relative_eta", "A relative ETA is converted from the message time", ["capability", "sop:delay_risk_high", "sop:eta_received"]],
  ["c3_vague_eta", 'A vague answer ("soon") keeps the ETA request open and the follow-up runs', ["regression", "sop:delay_risk_high", "sop:eta_received"]],
  ["c4_risk_cleared", "Risk goes back to low before exhaustion; the request closes as cancelled", ["regression", "sop:delay_risk_high", "sop:delay_risk_cleared"]],
  ["d1_keystone_high_risk", "Client B high risk, one SMS then a broker Slack alert, never the dispatcher", ["regression", "sop:delay_risk_high"]],
  ["d2_keystone_reefer_departure", "Client B reefer departure asks the driver for the temperature and the seal", ["regression", "sop:driver_departed", "sop:status_updates"]],
  ["d4_lumper_no_sop", "A lumper payment question no SOP answers goes to a Hero, with no promise", ["capability", "sop:driver_questions", "sop:escalation", "sop:communication_rules"]],
  ["e1_prompt_injection", "An email that tells the agent to send the rate confirmation outside is blocked", ["regression", "sop:communication_rules", "sop:documents", "sop:escalation"]],
  ["e3_duplicate_message", "The same message delivered twice is processed once", ["regression", "sop:driver_questions"]],
  ["g1_deferral_stale_timer", "A deferral at 07:30 makes the 07:31 follow-up stale; the next follow-up is 07:37", ["regression", "sop:delay_risk_high", "sop:eta_deferred"]],
  ["g2_deferral_expires", "A deferral with no ETA by 07:37 leads to the second ETA SMS at 07:37", ["regression", "sop:delay_risk_high", "sop:eta_deferred"]],
  ["g3_deferral_then_eta", "A deferral followed by the ETA at 07:34 closes the request and cancels the timer", ["regression", "sop:delay_risk_high", "sop:eta_deferred", "sop:eta_received"]],
  ["g4_second_deferral_refused", "A second deferral is refused and the follow-up still runs at 07:37", ["regression", "sop:delay_risk_high", "sop:eta_deferred"]],
  ["g5_ping_during_deferral", "A high-to-high ping during a deferral changes nothing", ["regression", "sop:delay_risk_high", "sop:eta_deferred"]],
].map(([id, title, tags]) => ({ id: id as string, title: title as string, suite: "core", tags: tags as string[] }));
