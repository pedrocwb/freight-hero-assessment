import { describe, expect, it } from "vitest";

import { ApiError, brokerInput, createApi } from "../api";
import { decideIntent } from "../lib/sopDraft";
import { mockFetch } from "./index";

// The mock backend answers the same contract as the API, so the screens can be laid out
// on it. These walk the plan's onboarding demo through it, end to end.
const api = createApi(mockFetch);

describe("mock mode: brokers and the SOP studio", () => {
  it("seeds Acme at config version 2 with a draft that waits for a decision", async () => {
    const brokers = await api.listBrokers();
    expect(brokers.map((b) => b.broker_id).sort()).toEqual(["acme", "keystone", "northline", "standard"]);
    const versions = await api.brokerVersions("acme");
    expect(versions.map((v) => [v.config_version, v.current])).toEqual([
      [2, true],
      [1, false],
    ]);
    const draft = await api.sopDraft("acme");
    expect(draft?.proposed_intents.map((p) => [p.intent_id, p.accepted])).toEqual([["detention_request", null]]);
    expect(draft?.proposed_requirements).toEqual([{ type_id: "pod", at_stage: "at_delivery", within_minutes: 120, opens_obligation: "pod" }]);
    expect(draft?.lint).toHaveLength(1);
    expect(await api.sopDraft("northline")).toBeNull();
  });

  it("publishes once the intent is accepted: a new config version with the intent and the POD", async () => {
    const draft = await api.sopDraft("acme");
    const accepted = decideIntent(draft!, "detention_request", true);
    const saved = await api.saveSopDraft("acme", {
      sections: accepted.sections,
      proposed_intents: accepted.proposed_intents,
      proposed_requirements: accepted.proposed_requirements,
      proposed_obligations: accepted.proposed_obligations,
    });
    expect(saved.lint).toEqual([]);
    const published = await api.publishSop("acme", { percent: 20 });
    expect(published.config_version).toBe(3);
    const acme = await api.getBroker("acme");
    expect(acme.rollout).toEqual({ percent: 20, previous_version: 2 });
    expect(acme.intents.map((i) => i.intent_id)).toEqual(expect.arrayContaining(["detention_request", "pod_follow_up_due"]));
    expect(acme.obligations.map((o) => o.kind)).toEqual(["eta_request", "pod"]);
    expect(acme.documents).toEqual([{ type_id: "pod", at_stage: "at_delivery", within_minutes: 120, opens_obligation: "pod" }]);
    expect((await api.sopVersions("acme"))[0]?.version).toBe(published.version);
    expect((await api.searchSops({ brokerId: "acme", loadTypes: [], q: "detention free time" }))[0]?.section_id).toBe("detention_request");
  });

  it("rolls back to 0% and refuses an unknown previous version with a 409", async () => {
    const rolled = await api.setRollout("acme", { percent: 0 });
    expect(rolled.rollout).toEqual({ percent: 0, previous_version: 2 });
    const error = await api.setRollout("acme", { percent: 50, previous_version: 9 }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
  });

  it("creates a broker as a copy of the standard one and saves an edit as a new version", async () => {
    const created = await api.createBroker({ broker_id: "pacific", name: "Pacific Freight" });
    expect(created.config_version).toBe(1);
    expect(created.intents).toHaveLength(12);
    const saved = await api.updateBroker("pacific", { ...brokerInput(created), limits: { ...created.limits, max_steps: 0 } });
    expect(saved.config_version).toBe(2);
    expect(saved.limits.max_steps).toBe(0);
    await expect(api.createBroker({ broker_id: "pacific", name: "Again" })).rejects.toMatchObject({ status: 409 });
  });
});

describe("mock mode: the agent's trajectory and documents", () => {
  it("records accepted and rejected tool calls on the Acme load", async () => {
    const runs = await api.runs("730118");
    const first = runs[0]!;
    expect(first).toMatchObject({ intent: "delay_risk_high", broker_id: "acme", broker_config_version: 2, rejections: 1 });
    expect(first.trajectory.map((s) => [s.tool, s.accepted])).toEqual([
      ["search_sop", null],
      ["read_sop_section", null],
      ["get_ledger", null],
      ["send_sms", true],
      ["send_email", false],
      ["schedule_timer", true],
      ["finish", null],
    ]);
  });

  it("reads an attachment into a pending document, processed on the next tick", async () => {
    const before = await api.loadDocuments("730118");
    expect(before.map((d) => [d.type_id, d.status, d.checks.every((c) => c.ok)])).toEqual([["pod", "processed", true]]);
    const upload = await api.uploadAttachment("481207", {
      file: new File(["%PDF"], "pod_exceptions.pdf", { type: "application/pdf" }),
      channel: "sms",
      senderContactId: "drv_20931",
      body: "",
      at: "2026-10-13T08:02:00-07:00",
    });
    expect((await api.getDocument(upload.document_id)).status).toBe("pending");
    await api.tick();
    const doc = await api.getDocument(upload.document_id);
    expect(doc.status).toBe("needs_review");
    expect(doc.checks.some((c) => !c.ok)).toBe(true);
  });
});
