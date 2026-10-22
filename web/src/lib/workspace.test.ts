import { describe, expect, it } from "vitest";
import {
  effectiveSops,
  operationPath,
  renderSop,
  type BrokerProfile,
  type SopSection,
} from "./workspace";
import { progress, type DurableSnapshot } from "./durable";
import { outbox } from "./testFixtures";

const base: SopSection = {
  section_id: "departed",
  title: "Departure",
  description: "",
  layer: "standard",
  client_id: null,
  load_type: null,
  mode: "replace",
  body: "Base.",
  source: "base",
  intents: [],
  stages: [],
};
describe("broker workspace", () => {
  it("previews replace and reefer extensions in precedence order", () => {
    const sections: SopSection[] = [
      base,
      { ...base, layer: "client", body: "Client." },
      {
        ...base,
        layer: "load_type",
        load_type: "reefer",
        mode: "extend",
        body: "Temperature and seal.",
      },
    ];
    expect(effectiveSops(sections)[0]?.body).toBe("Client.");
    expect(effectiveSops(sections, "reefer")[0]?.body).toBe(
      "Client.\n\nTemperature and seal.",
    );
    expect(base.body).toBe("Base.");
  });
  it("renders the same settings the user saves and scopes load URLs", () => {
    const profile = {
      broker: {
        obligations: [
          {
            kind: "eta_request",
            max_attempts: 1,
            follow_up_minutes: 30,
            on_exhausted: ["broker_slack"],
          },
        ],
      },
    } as BrokerProfile;
    expect(
      renderSop(
        "{{eta_request.max_attempts}} / {{eta_request.follow_up_minutes}} / {{eta_request.on_exhausted}}",
        profile,
      ),
    ).toBe("1 / 30 / Slack do broker");
    expect(operationPath("keystone", "load/1")).toBe(
      "/workspace/brokers/keystone/loads/load%2F1",
    );
  });
  it("recognizes Client B Slack escalation without requiring a Hero", () => {
    const state = {
      load: {
        sop_snapshot: {
          broker: {
            obligations: [
              {
                kind: "eta_request",
                max_attempts: 1,
                follow_up_minutes: 30,
                on_exhausted: ["broker_slack"],
              },
            ],
          },
        },
      },
      outbox: [
        outbox({
          outbox_id: "sms",
          run_id: "run",
          created_at: "2026-10-21T12:00:00Z",
          kind: "sms",
          payload: { purpose: "eta_request" },
        }),
        outbox({
          outbox_id: "slack",
          run_id: "run",
          created_at: "2026-10-21T12:30:00Z",
          kind: "broker_slack",
        }),
      ],
      obligations: [{ kind: "eta_request", status: "exhausted" }],
      timers: [],
      runs: [],
    } as unknown as DurableSnapshot;
    expect(progress(state)).toMatchObject({
      complete: true,
      maxAttempts: 1,
      intervalMinutes: 30,
    });
    state.outbox[1]!.status = "pending";
    expect(progress(state).complete).toBe(false);
  });
});
