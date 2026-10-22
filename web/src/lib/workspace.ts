import type { Stage } from "../domain";

export interface BrokerInfo {
  id: string;
  name: string;
  description: string;
}
export interface ObligationConfig {
  kind: string;
  title: string;
  description: string;
  max_attempts: number;
  follow_up_minutes: number;
  follow_up_intent: string;
  on_exhausted: string[];
}
export interface SopSection {
  section_id: string;
  layer: "standard" | "client" | "load_type";
  client_id: string | null;
  load_type: string | null;
  title: string;
  description: string;
  intents: string[];
  stages: Stage[];
  mode: "replace" | "extend";
  body: string;
  source: string;
}
export interface BrokerProfile {
  revision: number;
  broker: {
    broker_id: string;
    name: string;
    obligations: ObligationConfig[];
    intents: { intent_id: string; title: string }[];
    [key: string]: unknown;
  };
  sections: SopSection[];
}
export interface ScenarioStep {
  at: string;
  note: string;
  ping: { delay_risk: string; lat: number; lon: number } | null;
  message: {
    sender: string | null;
    channel: string;
    body: string;
    attachments: { filename: string }[];
    message_id: string | null;
  } | null;
}
export interface LiveScenario {
  id: string;
  title: string;
  description: string;
  suite: string;
  load_types: string[];
  stage: string;
  steps: ScenarioStep[];
}
export interface LiveLoad {
  load_id: string;
  stage: string;
  stopped: boolean;
  stopping: boolean;
  scenario: string;
  version: string;
  started_at: string | null;
}
export const brokerPath = (broker: string) =>
  `/workspace/brokers/${encodeURIComponent(broker)}`;
export const operationPath = (broker: string, load: string) =>
  `${brokerPath(broker)}/loads/${encodeURIComponent(load)}`;
export const targetNames: Record<string, string> = {
  dispatcher_email: "E-mail ao dispatcher",
  hero_task: "Tarefa para o hero",
  broker_slack: "Slack do broker",
};
export const stageNames: Record<string, string> = {
  dispatched: "Despachada",
  to_pickup: "A caminho da coleta",
  at_pickup: "Na coleta",
  to_delivery: "A caminho da entrega",
  at_delivery: "Na entrega",
  delivered: "Entregue",
};

/** Same precedence as the domain, used only for the editor preview. */
export function effectiveSops(
  sections: SopSection[],
  loadType = "",
): SopSection[] {
  const merged = new Map<string, SopSection>();
  for (const layer of ["standard", "client", "load_type"] as const) {
    for (const section of sections.filter(
      (s) =>
        s.layer === layer &&
        (layer !== "load_type" || s.load_type === loadType),
    )) {
      const base = merged.get(section.section_id);
      merged.set(
        section.section_id,
        base && section.mode === "extend"
          ? { ...base, body: `${base.body.trimEnd()}\n\n${section.body}` }
          : section,
      );
    }
  }
  return [...merged.values()].sort((a, b) =>
    a.section_id.localeCompare(b.section_id),
  );
}

export function renderSop(body: string, profile: BrokerProfile): string {
  for (const obligation of profile.broker.obligations) {
    body = body
      .replaceAll(
        `{{${obligation.kind}.max_attempts}}`,
        String(obligation.max_attempts),
      )
      .replaceAll(
        `{{${obligation.kind}.follow_up_minutes}}`,
        String(obligation.follow_up_minutes),
      )
      .replaceAll(
        `{{${obligation.kind}.on_exhausted}}`,
        obligation.on_exhausted.map((t) => targetNames[t] || t).join(" → "),
      );
  }
  return body;
}
