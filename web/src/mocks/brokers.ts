/**
 * Brokers and their knowledge base for mock mode, as cards BRK and KNW would serve them:
 * the standard broker and the two imported clients (Northline, Keystone), and Acme,
 * onboarded the way the plan's demo does it. Acme was created as a copy of the standard
 * broker (config version 1), published a short Markdown SOP (version 2), then uploaded
 * its full PDF SOP: that draft waits for review with a proposed `detention_request`
 * intent and a proposed POD requirement.
 */

import type {
  Broker,
  BrokerVersionView,
  DocumentTypeDef,
  DraftSection,
  IntentDef,
  ObligationDef,
  SearchHit,
  SopDocumentView,
  SopDraftView,
  SopVersionView,
} from "../api";
import { draftProblems } from "../lib/sopDraft";
import { HttpError } from "./http";
import type { MockSopSection } from "./sopData";
import { at } from "./story";

// --- the standard broker (domain.broker.standard_broker) --------------------------------

function intent(
  intent_id: string,
  title: string,
  description: string,
  more: Partial<IntentDef> = {},
): IntentDef {
  return { intent_id, title, description, examples: [], priority: 100, must_act: true, noop_eligible: false, system: false, ...more };
}

const STANDARD_INTENTS: IntentDef[] = [
  intent("eta_provided", "ETA provided", 'Gives an arrival time at the next stop, as a clock time or a duration. Also a vague reply to our ETA request ("soon", "on my way"); the ETA procedure judges if it is precise enough.', { priority: 10, examples: ["be there 9:40", "30 mins out", "ETA 10am"] }),
  intent("eta_deferred", "ETA promised for later", "Promises the ETA or the answer later.", { priority: 20, examples: ["I'll tell you in 5 min", "let me check and get back to you"] }),
  intent("question", "Question", "Asks us something.", { priority: 30, examples: ["What's the PO number?", "Where do I check in?", "Is the appointment still 9:30?"] }),
  intent("driver_departed", "Driver departed a stop", 'The truck has left or is leaving a stop. "loaded, heading out" is a departure, not a status update.', { priority: 40, examples: ["leaving the shipper now", "loaded, heading out", "just left the receiver"] }),
  intent("document", "Document", "Sends a document or is mainly about one (BOL, POD, rate confirmation, receipt). A message with an attachment is usually a document.", { priority: 50, examples: ["here is the BOL", "a photo with no text"] }),
  intent("status_update", "Status update", "Other news about the load or the truck that is not an ETA and not a departure.", { priority: 60, must_act: false, examples: ["at the gate", "stuck in traffic", "running late", "waiting for paperwork"] }),
  intent("acknowledgement", "Acknowledgement", "Only acknowledges, with nothing new and no question. An acknowledgement is never an ETA.", { priority: 70, must_act: false, noop_eligible: true, examples: ["ok", "thanks", "got it"] }),
  intent("other", "Other", "Anything else: a wrong number, small talk, a request that no intent above covers.", { priority: 1000, must_act: false, examples: ["wrong number", "how's the weather there?"] }),
  intent("delay_risk_high", "Delay risk is high", "A ping moved the load's delay risk into high. Set by code, no gate call.", { system: true }),
  intent("delay_risk_low", "Delay risk is back to normal", "A ping moved the delay risk out of high. Set by code, no gate call.", { system: true }),
  intent("eta_follow_up_due", "ETA follow-up due", "The follow-up timer of an open ETA request came due. Set by code, no gate call.", { system: true }),
  intent("document_processed", "Document processed", "The document pipeline finished a document of this load: its type, its values and the checks against the load. Set by code, no gate call.", { system: true }),
];

const ETA_REQUEST: ObligationDef = {
  kind: "eta_request",
  title: "ETA request",
  description: "We asked a contact for the ETA at the next stop and wait for it.",
  follow_up_minutes: 30,
  max_attempts: 2,
  follow_up_intent: "eta_follow_up_due",
  on_exhausted: ["dispatcher_email", "hero_task"],
};

const field = (name: string, description: string, kind: DocumentTypeDef["fields"][number]["kind"] = "text", check: DocumentTypeDef["fields"][number]["check"] = "none") => ({ name, description, kind, check });

const DOCUMENT_TYPES: DocumentTypeDef[] = [
  {
    type_id: "bol",
    title: "Bill of lading",
    description: "Signed at pickup: what was loaded, for whom, under which seal.",
    fields: [
      field("bol_number", "The BOL number"),
      field("shipper", "Who shipped the load", "text", "stop_name"),
      field("consignee", "Who receives the load"),
      field("po_numbers", "PO numbers on the BOL", "text", "po_number"),
      field("seal_number", "The trailer seal", "text", "seal_number"),
      field("weight_lb", "Gross weight in pounds", "number"),
      field("pickup_date", "Date of the pickup", "date", "date_in_window"),
      field("signed", "Signed by the shipper and the driver", "bool", "signature"),
    ],
  },
  {
    type_id: "pod",
    title: "Proof of delivery",
    description: "Signed at delivery: who received the load, when, and any exception.",
    fields: [
      field("receiver", "The receiving facility", "text", "stop_name"),
      field("signed_by", "Name of the person who signed", "text", "signature"),
      field("delivered_at", "Delivery time", "datetime", "date_in_window"),
      field("po_number", "PO number on the POD", "text", "po_number"),
      field("exceptions", "Shortage, damage or refusal noted on the POD"),
    ],
  },
  {
    type_id: "rate_confirmation",
    title: "Rate confirmation",
    description: "The carrier's agreed rate. Never forwarded, its prices never repeated.",
    fields: [
      field("carrier", "Carrier name"),
      field("mc_number", "Carrier MC number", "text", "mc_number"),
      field("rate_usd", "Total rate", "number"),
      field("stops", "Stops and appointments"),
    ],
  },
  {
    type_id: "receipt",
    title: "Receipt",
    description: "A lumper, scale or fuel receipt.",
    fields: [
      field("kind", "Lumper, scale or fuel"),
      field("amount_usd", "Amount", "number"),
      field("date", "Date of the receipt", "date", "date_in_window"),
      field("vendor", "Who issued it"),
    ],
  },
];

export function standardBroker(): Broker {
  return {
    broker_id: "standard",
    name: "Standard procedure",
    triage: true,
    intents: STANDARD_INTENTS.map((i) => ({ ...i, examples: [...i.examples] })),
    obligations: [{ ...ETA_REQUEST, on_exhausted: [...ETA_REQUEST.on_exhausted] }],
    document_types: DOCUMENT_TYPES.map((t) => ({ ...t, fields: t.fields.map((f) => ({ ...f })) })),
    documents: [],
    limits: {
      validation: {
        sms_max_per_window: 3,
        sms_window_minutes: 60,
        deferral: { max_minutes: 15, max_per_obligation: 1, grace_minutes: 2 },
        max_attempts: { eta_request: 2 },
      },
      max_steps: 8,
      max_cost_usd: 0.05,
      max_rejections: 3,
    },
    sop_version: "c41f9a7e2b13",
    config_version: 1,
    rollout: { percent: 100, previous_version: null },
  };
}

/** `domain.broker.new_broker`: a copy of the standard broker under a new id and name. */
export function newBroker(brokerId: string, name: string): Broker {
  return { ...standardBroker(), broker_id: brokerId, name, sop_version: null };
}

// --- the knowledge base ------------------------------------------------------------------

const ACME_MD: SopDocumentView = {
  document_id: "sopdoc_acme_1",
  broker_id: "acme",
  filename: "acme_dispatch_notes.md",
  content_type: "text/markdown",
  pages: 1,
  sha256: "5b0e93c1d2a47f18c3e0b9d6a2f41c87e5d03b6a9f2c4e18d7b0a3c5e9f1d2a4",
  uploaded_at: at("06:08"),
};

const ACME_PDF: SopDocumentView = {
  document_id: "sopdoc_acme_2",
  broker_id: "acme",
  filename: "acme_sop.pdf",
  content_type: "application/pdf",
  pages: 6,
  sha256: "a3c9e1f04b7d28c6e5f1a0b9d3c7e2f48a6b1d0c9e3f5a7b2d4c6e8f0a1b3c5d",
  uploaded_at: at("06:30"),
};

const ACME_DELAY: Omit<DraftSection, "source" | "proposed"> = {
  section_id: "delay_risk_high",
  title: "Delay risk changes to high (Acme)",
  description: "One SMS to the driver, a second after 20 minutes, then email the dispatcher and create a Hero task.",
  intents: ["delay_risk_high", "eta_follow_up_due"],
  stages: ["to_pickup", "to_delivery"],
  mode: "replace",
  body: "Acme's procedure. It replaces the standard one.\n\nWhen the delay risk changes to high:\n1. If an ETA request is already open, do nothing.\n2. Otherwise text the driver once for the ETA at the next stop. This is ETA request 1.\n3. Follow up in 20 minutes.\n\nWhen the follow-up is due and there is still no ETA:\n- After 1 request: text the driver again and follow up in 20 minutes.\n- After 2 requests: email the dispatcher in the main thread, then create an urgent Hero task to call the driver. Close the ETA request as exhausted.\n\nNever write to the receiver or the shipper: only the load's own contacts.",
};

const ACME_SIGNATURE: Omit<DraftSection, "source" | "proposed"> = {
  section_id: "communication_rules",
  title: "",
  description: "",
  intents: [],
  stages: [],
  mode: "extend",
  body: "Acme adds one rule: sign every SMS as \"Acme Freight dispatch\".",
};

const ACME_DETENTION: Omit<DraftSection, "source" | "proposed"> = {
  section_id: "detention_request",
  title: "Detention at a stop",
  description: "The driver waits more than 2 hours at a facility: log the times and hand the request to a Hero.",
  intents: ["detention_request"],
  stages: ["at_pickup", "at_delivery"],
  mode: "replace",
  body: "Detention is paid waiting time after the free time at a facility. Acme's free time is 2 hours from the appointment or the check-in, whichever is later.\n\nWhen the driver or the dispatcher reports waiting beyond the free time, or asks for detention:\n1. Write a TMS note with the check-in time and the time now, as local clock times.\n2. Create a Hero task, not urgent, to approve the detention with the facility. Write the load number, the stop, the check-in time and who asked.\n3. Never promise detention pay to the driver: reply only that the request was passed on.",
};

const ACME_POD: Omit<DraftSection, "source" | "proposed"> = {
  section_id: "documents",
  title: "",
  description: "",
  intents: [],
  stages: [],
  mode: "extend",
  body: "Acme needs the signed POD within 2 hours of the arrival at the receiver.\n- If the POD has no exception and its checks pass, set the stage to delivered and write a TMS note with who signed and when.\n- If the POD notes a shortage, damage or a refusal, create an urgent Hero task with the values read from the POD.\n- If no POD arrives within 2 hours, ask the driver once by SMS for a photo of the signed POD.",
};

function draftSections(): DraftSection[] {
  return [
    { ...ACME_DELAY, source: { document_id: ACME_PDF.document_id, pages: [2] }, proposed: false },
    { ...ACME_SIGNATURE, source: { document_id: ACME_PDF.document_id, pages: [1] }, proposed: false },
    { ...ACME_DETENTION, source: { document_id: ACME_PDF.document_id, pages: [3, 4] }, proposed: true },
    { ...ACME_POD, source: { document_id: ACME_PDF.document_id, pages: [5] }, proposed: true },
  ];
}

function acmeDraft(brokerIntents: string[], documentTypes: string[]): SopDraftView {
  const draft: SopDraftView = {
    broker_id: "acme",
    sections: draftSections(),
    proposed_intents: [
      {
        intent_id: "detention_request",
        title: "Detention request",
        description: "The driver or the dispatcher reports waiting at a facility beyond the free time, or asks for detention pay.",
        examples: ["We've been waiting 3 hours at the dock, can we get detention?", "Detention started at 10:15"],
        priority: 35,
        must_act: true,
        noop_eligible: false,
        system: false,
        evidence: [
          'Page 3: "When a driver reports more than 2 hours of waiting at a facility, log the arrival and the departure times and open a detention request with the broker team."',
          'Page 4: "Detention requests are approved by a person, never promised to the driver by the dispatcher."',
        ].join("\n"),
        accepted: null,
      },
    ],
    proposed_requirements: [{ type_id: "pod", at_stage: "at_delivery", within_minutes: 120, opens_obligation: "pod" }],
    proposed_obligations: [
      {
        kind: "pod",
        title: "POD request",
        description: "The signed POD the load owes once the truck is at the receiver.",
        follow_up_minutes: 120,
        max_attempts: 1,
        follow_up_intent: "pod_follow_up_due",
        on_exhausted: ["hero_task"],
      },
    ],
    document_ids: [ACME_PDF.document_id],
    updated_at: at("06:31"),
    lint: [],
    documents: [ACME_PDF],
  };
  return { ...draft, lint: draftProblems(draft, brokerIntents, documentTypes, ["eta_request"]) };
}

/** A broker's own sections as the SOP viewer merges them over the standard layer. */
function asMockSections(brokerId: string, sections: readonly DraftSection[], documents: readonly SopDocumentView[]): MockSopSection[] {
  return sections.map((s) => {
    const doc = documents.find((d) => d.document_id === s.source?.document_id);
    return {
      section_id: s.section_id,
      layer: "client",
      client_id: brokerId,
      load_type: s.load_type ?? null,
      title: s.title,
      description: s.description,
      intents: [...s.intents],
      stages: [...s.stages],
      mode: s.mode,
      body: s.body,
      source: doc ? `${doc.filename}${s.source?.pages.length ? ` p.${s.source.pages.join(",")}` : ""}` : `brokers/${brokerId}`,
    };
  });
}

// --- the world -----------------------------------------------------------------------------

export interface BrokerWorld {
  brokers: Map<string, Broker>;
  /** Every saved version of each broker, oldest first (`broker_versions`). */
  versions: Map<string, Broker[]>;
  drafts: Map<string, SopDraftView>;
  sopVersions: Map<string, SopVersionView[]>;
  sopDocuments: SopDocumentView[];
  /** Each broker's published sections (its client layer). */
  published: Map<string, MockSopSection[]>;
}

const IMPORTED_AT = at("06:00");

export function seedBrokerWorld(): BrokerWorld {
  const standard = standardBroker();
  const northline: Broker = { ...standardBroker(), broker_id: "northline", name: "Northline Logistics" };
  const keystone: Broker = {
    ...standardBroker(),
    broker_id: "keystone",
    name: "Keystone Freight",
    obligations: [{ ...ETA_REQUEST, max_attempts: 1, on_exhausted: ["broker_slack"] }],
  };
  keystone.limits = { ...keystone.limits, validation: { ...keystone.limits.validation, max_attempts: { eta_request: 1 } } };
  const acme1: Broker = newBroker("acme", "Acme Freight");
  const acme: Broker = {
    ...acme1,
    obligations: [{ ...ETA_REQUEST, follow_up_minutes: 20, on_exhausted: ["dispatcher_email", "hero_task"] }],
    limits: { ...acme1.limits, max_steps: 6, max_cost_usd: 0.04 },
    sop_version: "5b0e93c1d2a4",
    config_version: 2,
    rollout: { percent: 100, previous_version: 1 },
  };

  // Acme first after the template: the onboarding screens open on the broker being onboarded.
  const brokers = new Map<string, Broker>([standard, acme, northline, keystone].map((b) => [b.broker_id, b]));
  const versions = new Map<string, Broker[]>([
    ["standard", [standard]],
    ["northline", [northline]],
    ["keystone", [keystone]],
    ["acme", [acme1, acme]],
  ]);

  const imported = (brokerId: string, version: string): SopVersionView => ({
    broker_id: brokerId,
    version,
    published_at: IMPORTED_AT,
    published_by: "agent sop import sops/",
    section_ids: ["acknowledgements", "communication_rules", "delay_risk_cleared", "delay_risk_high", "documents", "driver_departed", "driver_questions", "escalation", "eta_deferred", "eta_received", "status_updates"],
    lint: [],
  });
  const acmeV1Sections: DraftSection[] = [
    { ...ACME_DELAY, source: { document_id: ACME_MD.document_id, pages: [1] }, proposed: false },
    { ...ACME_SIGNATURE, source: { document_id: ACME_MD.document_id, pages: [1] }, proposed: false },
  ];
  const sopVersions = new Map<string, SopVersionView[]>([
    ["standard", [imported("standard", "c41f9a7e2b13")]],
    ["northline", [imported("northline", "c41f9a7e2b13")]],
    ["keystone", [imported("keystone", "c41f9a7e2b13")]],
    [
      "acme",
      [
        {
          broker_id: "acme",
          version: "5b0e93c1d2a4",
          published_at: at("06:12"),
          published_by: "pedro",
          section_ids: acmeV1Sections.map((s) => s.section_id),
          lint: [],
        },
      ],
    ],
  ]);

  const drafts = new Map<string, SopDraftView>([
    ["acme", acmeDraft(acme.intents.map((i) => i.intent_id), acme.document_types.map((t) => t.type_id))],
  ]);
  const sopDocuments = [ACME_MD, ACME_PDF];
  const published = new Map<string, MockSopSection[]>([["acme", asMockSections("acme", acmeV1Sections, sopDocuments)]]);
  return { brokers, versions, drafts, sopVersions, sopDocuments, published };
}

// --- operations (what the routes do) -----------------------------------------------------

export function findBroker(world: BrokerWorld, brokerId: string): Broker {
  const broker = world.brokers.get(brokerId);
  if (!broker) throw new HttpError(404, `broker ${brokerId} not found`);
  return broker;
}

/** `BrokerVersionView.of`: one saved version, newest first in the listing. */
export function versionViews(world: BrokerWorld, brokerId: string): BrokerVersionView[] {
  const current = findBroker(world, brokerId);
  return [...(world.versions.get(brokerId) ?? [])]
    .sort((a, b) => b.config_version - a.config_version)
    .map((b) => ({
      config_version: b.config_version,
      name: b.name,
      sop_version: b.sop_version,
      rollout: b.rollout,
      current: b.config_version === current.config_version,
      intents: b.intents.map((i) => i.intent_id),
      obligations: b.obligations.map((o) => o.kind),
    }));
}

/** Save a new config version: it goes live (at 100% unless told) with the old one kept for a rollback. */
function bump(world: BrokerWorld, broker: Broker, patch: Partial<Broker>, percent = 100): Broker {
  const next: Broker = {
    ...broker,
    ...patch,
    config_version: broker.config_version + 1,
    rollout: { percent, previous_version: broker.config_version },
  };
  next.limits = {
    ...next.limits,
    validation: {
      ...next.limits.validation,
      max_attempts: Object.fromEntries(next.obligations.map((o) => [o.kind, o.max_attempts])),
    },
  };
  world.brokers.set(next.broker_id, next);
  world.versions.set(next.broker_id, [...(world.versions.get(next.broker_id) ?? []), next]);
  return next;
}

type BrokerFields = Partial<Pick<Broker, "broker_id" | "name" | "triage" | "intents" | "obligations" | "document_types" | "documents" | "limits">>;

function editable(body: BrokerFields): BrokerFields {
  const { broker_id: _id, ...rest } = body;
  return Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== undefined && v !== null)) as BrokerFields;
}

export function createBroker(world: BrokerWorld, body: BrokerFields): Broker {
  const id = (body.broker_id ?? "").trim();
  const name = (body.name ?? "").trim();
  if (!/^[a-z][a-z0-9_]*$/.test(id)) throw new HttpError(422, "broker_id: use snake_case, starting with a letter");
  if (!name) throw new HttpError(422, "name: the broker needs a name");
  if (world.brokers.has(id)) throw new HttpError(409, `broker ${id} already exists`);
  const broker: Broker = { ...newBroker(id, name), ...editable(body), name };
  world.brokers.set(id, broker);
  world.versions.set(id, [broker]);
  return broker;
}

export function updateBroker(world: BrokerWorld, brokerId: string, body: BrokerFields): Broker {
  const broker = findBroker(world, brokerId);
  if (body.broker_id && body.broker_id !== brokerId) throw new HttpError(422, "broker_id: it is the path's id");
  const changes = editable(body);
  const intents = changes.intents ?? broker.intents;
  for (const def of broker.intents.filter((i) => i.system)) {
    const given = intents.find((i) => i.intent_id === def.intent_id);
    if (!given) throw new HttpError(422, `intents: the system intent ${def.intent_id} cannot be removed`);
    if (JSON.stringify(given) !== JSON.stringify(def)) throw new HttpError(422, `intents: the system intent ${def.intent_id} cannot be changed`);
  }
  const ids = intents.map((i) => i.intent_id);
  const twice = ids.find((id, i) => ids.indexOf(id) !== i);
  if (twice) throw new HttpError(422, `intents: ${twice} is listed twice`);
  return bump(world, broker, changes);
}

export function setRollout(world: BrokerWorld, brokerId: string, body: { percent?: number; previous_version?: number | null }): Broker {
  const broker = findBroker(world, brokerId);
  const percent = Number(body.percent);
  if (!Number.isInteger(percent) || percent < 0 || percent > 100) throw new HttpError(422, "percent: a whole number from 0 to 100");
  const previous = body.previous_version === undefined || body.previous_version === null ? broker.rollout.previous_version : body.previous_version;
  const known = (world.versions.get(brokerId) ?? []).some((v) => v.config_version === previous && v.config_version !== broker.config_version);
  if (previous !== null && !known) throw new HttpError(409, `broker ${brokerId} has no previous version ${previous}`);
  const next = { ...broker, rollout: { percent, previous_version: previous } };
  world.brokers.set(brokerId, next);
  world.versions.set(brokerId, (world.versions.get(brokerId) ?? []).map((v) => (v.config_version === next.config_version ? next : v)));
  return next;
}

function withLint(world: BrokerWorld, broker: Broker, draft: SopDraftView): SopDraftView {
  const documents = world.sopDocuments.filter((d) => draft.document_ids.includes(d.document_id));
  const next = { ...draft, documents };
  const lint = draftProblems(
    next,
    broker.intents.map((i) => i.intent_id),
    broker.document_types.map((t) => t.type_id),
    broker.obligations.map((o) => o.kind),
  );
  return { ...next, lint };
}

/**
 * An upload compiles the file into the broker's draft. Mock mode has no compile model, so
 * every file reads as Acme's PDF SOP: the same sections and proposals, from this file.
 */
export function uploadSop(world: BrokerWorld, brokerId: string, file: File | null, now: string) {
  const broker = findBroker(world, brokerId);
  if (!file) throw new HttpError(422, "file: attach a PDF or Markdown file");
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  const isMd = /\.(md|markdown)$/i.test(file.name) || file.type === "text/markdown";
  if (!isPdf && !isMd) throw new HttpError(415, `${file.name}: only PDF and Markdown SOPs can be read`);
  const document: SopDocumentView = {
    document_id: `sopdoc_${brokerId}_${world.sopDocuments.length + 1}`,
    broker_id: brokerId,
    filename: file.name,
    content_type: isPdf ? "application/pdf" : "text/markdown",
    pages: isPdf ? 6 : 1,
    sha256: Array.from({ length: 64 }, (_, i) => "0123456789abcdef"[(file.size + i * 7) % 16]).join(""),
    uploaded_at: now,
  };
  world.sopDocuments.push(document);
  const base = acmeDraft([], []);
  const sections = base.sections
    .filter((s) => broker.broker_id === "acme" || s.section_id !== "delay_risk_high")
    .map((s) => ({ ...s, source: s.source ? { document_id: document.document_id, pages: isPdf ? s.source.pages : [1] } : null }));
  const knownIntents = new Set(broker.intents.map((i) => i.intent_id));
  const knownKinds = new Set(broker.obligations.map((o) => o.kind));
  const draft: SopDraftView = {
    ...base,
    broker_id: brokerId,
    sections,
    proposed_intents: base.proposed_intents.filter((p) => !knownIntents.has(p.intent_id)),
    proposed_obligations: base.proposed_obligations.filter((o) => !knownKinds.has(o.kind)),
    proposed_requirements: base.proposed_requirements.filter((r) => !broker.documents.some((d) => d.type_id === r.type_id && d.at_stage === r.at_stage)),
    document_ids: [document.document_id],
    updated_at: now,
  };
  world.drafts.set(brokerId, withLint(world, broker, draft));
  return { document_id: document.document_id, pages: document.pages, status: "compiled" };
}

export function getDraft(world: BrokerWorld, brokerId: string): SopDraftView {
  const broker = findBroker(world, brokerId);
  const draft = world.drafts.get(brokerId);
  if (!draft) throw new HttpError(404, `broker ${brokerId} has no SOP draft: upload its SOP first`);
  return withLint(world, broker, draft);
}

export function saveDraft(world: BrokerWorld, brokerId: string, body: Partial<SopDraftView>, now: string): SopDraftView {
  const current = getDraft(world, brokerId);
  if (!Array.isArray(body.sections)) throw new HttpError(422, "sections: Field required");
  const next: SopDraftView = {
    ...current,
    sections: body.sections,
    proposed_intents: body.proposed_intents ?? [],
    proposed_requirements: body.proposed_requirements ?? [],
    proposed_obligations: body.proposed_obligations ?? [],
    updated_at: now,
  };
  const saved = withLint(world, findBroker(world, brokerId), next);
  world.drafts.set(brokerId, saved);
  return saved;
}

/**
 * `publish_draft`: lint, a new SOP version, and the broker saved with the accepted
 * intents, the proposed obligations (with their follow-up intents) and requirements.
 */
export function publishDraft(world: BrokerWorld, brokerId: string, body: { percent?: number }, now: string) {
  const broker = findBroker(world, brokerId);
  const draft = getDraft(world, brokerId);
  if (draft.lint.length) throw new HttpError(422, `the draft does not publish: ${draft.lint.join(" ")}`);
  const percent = body.percent ?? 100;
  if (!Number.isInteger(percent) || percent < 0 || percent > 100) throw new HttpError(422, "percent: a whole number from 0 to 100");
  const intents = [...broker.intents];
  for (const p of draft.proposed_intents) {
    if (p.accepted !== true || intents.some((i) => i.intent_id === p.intent_id)) continue;
    const { evidence: _e, accepted: _a, ...def } = p;
    intents.push(def);
  }
  const obligations = [...broker.obligations];
  for (const o of draft.proposed_obligations) {
    const at = obligations.findIndex((x) => x.kind === o.kind);
    if (at >= 0) obligations[at] = o;
    else obligations.push(o);
    const followUp = o.follow_up_intent || `${o.kind}_follow_up_due`;
    if (!intents.some((i) => i.intent_id === followUp)) {
      intents.push({
        intent_id: followUp,
        title: `${o.title} follow-up due`,
        description: `The follow-up timer of an open ${o.kind} obligation is due. Set by code, no gate call.`,
        examples: [],
        priority: 100,
        must_act: true,
        noop_eligible: false,
        system: true,
      });
    }
  }
  const documents = [
    ...broker.documents,
    ...draft.proposed_requirements.filter((r) => !broker.documents.some((d) => d.type_id === r.type_id && d.at_stage === r.at_stage)),
  ];
  const version = Array.from({ length: 12 }, (_, i) => "0123456789abcdef"[(draft.sections.length * 31 + now.length * 7 + i * 11 + broker.config_version * 5) % 16]).join("");
  const sections = draft.sections.map((s) => ({ ...s, proposed: false }));
  world.sopVersions.set(brokerId, [
    ...(world.sopVersions.get(brokerId) ?? []),
    { broker_id: brokerId, version, published_at: now, published_by: "pedro", section_ids: sections.map((s) => s.section_id), lint: [] },
  ]);
  world.published.set(brokerId, asMockSections(brokerId, sections, world.sopDocuments));
  world.drafts.set(brokerId, { ...draft, sections, proposed_obligations: [], proposed_requirements: [], updated_at: now });
  const next = bump(world, broker, { intents, obligations, documents, sop_version: version }, percent);
  return { version, config_version: next.config_version, lint: [] as string[] };
}

export function sopVersions(world: BrokerWorld, brokerId: string): SopVersionView[] {
  findBroker(world, brokerId);
  return [...(world.sopVersions.get(brokerId) ?? [])].reverse();
}

const tokens = (text: string) => text.toLowerCase().match(/[a-z0-9]+/g) ?? [];

/** Word overlap over title, description and body, like `MemorySopIndex.search`. */
export function search(sections: readonly { section_id: string; title: string; description: string; body: string }[], q: string, k = 5): SearchHit[] {
  const wanted = new Set(tokens(q).filter((t) => t.length > 2));
  if (!wanted.size) return [];
  return sections
    .map((s) => {
      const have = new Set(tokens(`${s.title} ${s.description} ${s.body}`));
      const hits = [...wanted].filter((t) => have.has(t)).length;
      return { section_id: s.section_id, title: s.title || s.section_id, description: s.description, score: Math.round((hits / wanted.size) * 1000) / 1000 };
    })
    .filter((h) => h.score > 0)
    .sort((a, b) => b.score - a.score || a.section_id.localeCompare(b.section_id))
    .slice(0, k);
}
