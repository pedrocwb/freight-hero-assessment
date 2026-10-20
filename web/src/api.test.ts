import { describe, expect, it } from "vitest";

import {
  ApiError,
  attachmentForm,
  brokerInput,
  buildRequest,
  buildUrl,
  createApi,
  draftInput,
  errorDetail,
  inboundMessage,
  runCost,
  type Broker,
  type Fetcher,
  type SopDraft,
} from "./api";
import { newBroker } from "./mocks/brokers";

interface Call {
  url: string;
  init: RequestInit | undefined;
}

function recorder(status = 200, body: unknown = {}) {
  const calls: Call[] = [];
  const fetcher: Fetcher = async (url, init) => {
    calls.push({ url, init });
    const text = typeof body === "string" ? body : JSON.stringify(body);
    return new Response(text, { status, headers: { "Content-Type": "application/json" } });
  };
  return { calls, api: createApi(fetcher, "/api") };
}

const bodyOf = (call: Call | undefined) => JSON.parse(String(call?.init?.body));

describe("buildUrl", () => {
  it("leaves out empty values and joins lists with commas", () => {
    expect(
      buildUrl("/api", "/sops", { client_id: "keystone", load_types: ["reefer", " hazmat "], x: "" }),
    ).toBe("/api/sops?client_id=keystone&load_types=reefer%2Chazmat");
  });

  it("drops an empty list and null values, and needs no question mark then", () => {
    expect(buildUrl("/api", "/sops", { load_types: [], intent: null, stage: undefined })).toBe(
      "/api/sops",
    );
  });

  it("encodes reserved characters", () => {
    expect(buildUrl("/api", "/x", { note: "a&b c" })).toBe("/api/x?note=a%26b+c");
  });
});

describe("buildRequest", () => {
  it("sends JSON with a content type only when there is a body", () => {
    const get = buildRequest("/api", { method: "GET", path: "/sim/state" });
    expect(get.init.body).toBeUndefined();
    expect(get.init.headers).toEqual({ Accept: "application/json" });

    const post = buildRequest("/api", { method: "POST", path: "/sim/advance", body: { minutes: 5 } });
    expect(post.init.method).toBe("POST");
    expect(post.init.body).toBe('{"minutes":5}');
    expect(post.init.headers).toMatchObject({ "Content-Type": "application/json" });
  });
});

describe("api client", () => {
  it("advances by minutes or to an instant", async () => {
    const { calls, api } = recorder(200, { state: {}, runs: [] });
    await api.advance({ minutes: 30 });
    await api.advance({ to: "2026-10-13T07:31:00-07:00" });
    expect(calls[0]?.url).toBe("/api/sim/advance");
    expect(bodyOf(calls[0])).toEqual({ minutes: 30 });
    expect(bodyOf(calls[1])).toEqual({ to: "2026-10-13T07:31:00-07:00" });
  });

  it("posts a driver SMS as an InboundMessage with the latency as a query parameter", async () => {
    const { calls, api } = recorder(200, { accepted: true, duplicate: false, event_id: "m1", reason: null });
    const message = inboundMessage({
      loadId: "481207",
      channel: "sms",
      senderContactId: "drv_20931",
      body: "  What's the PO number?  ",
      at: "2026-10-13T07:12:00-07:00",
    });
    await api.sendMessage(message, 60);
    expect(calls[0]?.url).toBe("/api/v1/events?latency_seconds=60");
    expect(bodyOf(calls[0])).toEqual({
      load_id: "481207",
      channel: "sms",
      sender_contact_id: "drv_20931",
      body: "What's the PO number?",
      occurred_at: "2026-10-13T07:12:00-07:00",
    });
    // The API fills the message id; sending one would make every retry a duplicate.
    expect(bodyOf(calls[0])).not.toHaveProperty("message_id");
  });

  it("asks for one conversation channel and escapes the load id", async () => {
    const { calls, api } = recorder(200, []);
    await api.conversation("481/207", "email");
    expect(calls[0]?.url).toBe("/api/loads/481%2F207/conversation?channel=email");
  });

  it("builds the hero task and SOP preview queries", async () => {
    const { calls, api } = recorder(200, []);
    await api.heroTasks("open");
    await api.completeHeroTask("out_5", "called the driver, ETA 9:40");
    await api.sopPreview({
      clientId: "keystone",
      loadTypes: ["reefer"],
      intent: "driver_departed",
      stage: "to_delivery",
    });
    expect(calls[0]?.url).toBe("/api/hero/tasks?status=open");
    expect(calls[1]?.url).toBe("/api/hero/tasks/out_5/complete");
    expect(bodyOf(calls[1])).toEqual({ note: "called the driver, ETA 9:40" });
    expect(calls[2]?.url).toBe(
      "/api/sops/preview?client_id=keystone&load_types=reefer&intent=driver_departed&stage=to_delivery",
    );
  });

  it("normalizes the eval summary it gets back", async () => {
    const { calls, api } = recorder(200, {
      suite: "core",
      scenarios: [{ scenario_id: "a", passed: true, pass_k: 1, failures: [], cost_usd: 0.01 }],
    });
    const summary = await api.runEvals({ suite: "core", mode: "replay", k: 1, scenario: null });
    expect(bodyOf(calls[0])).toEqual({ suite: "core", mode: "replay", k: 1, scenario: null });
    expect(summary.passed).toBe(1);
    expect(summary.scenarios[0]?.scenarioId).toBe("a");
  });

  it("raises the API's detail as the error message", async () => {
    const { api } = recorder(400, { detail: "invalid load metadata: 'stops'" });
    await expect(api.createLoad({ load_id: "x" })).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      detail: "invalid load metadata: 'stops'",
    });
  });

  it("reports an unreachable API as status 0", async () => {
    const api = createApi(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = await api.simState().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });
});

const formOf = (call: Call | undefined) => call?.init?.body as FormData;

describe("multipart requests", () => {
  it("sends a form as it is, without a JSON content type, so the browser sets the boundary", () => {
    const form = new FormData();
    form.append("file", new Blob(["%PDF"], { type: "application/pdf" }), "acme_sop.pdf");
    const { init } = buildRequest("/api", { method: "POST", path: "/brokers/acme/sops", form });
    expect(init.body).toBe(form);
    expect(init.headers).toEqual({ Accept: "application/json" });
  });

  it("builds the attachment form: the file and the message it came with", () => {
    const form = attachmentForm({
      file: new Blob(["%PDF"], { type: "application/pdf" }),
      filename: "pod_clean.pdf",
      channel: "sms",
      senderContactId: "drv_20931",
      body: "  Signed POD attached ",
      at: "2026-10-13T07:52:00-07:00",
    });
    expect((form.get("file") as File).name).toBe("pod_clean.pdf");
    expect(form.get("channel")).toBe("sms");
    expect(form.get("sender_contact_id")).toBe("drv_20931");
    expect(form.get("body")).toBe("Signed POD attached");
    expect(form.get("occurred_at")).toBe("2026-10-13T07:52:00-07:00");
  });

  it("leaves the sender out when the message has none", () => {
    const form = attachmentForm({ file: new Blob(["x"]), channel: "email", senderContactId: null, body: "", at: "t" });
    expect(form.has("sender_contact_id")).toBe(false);
    expect((form.get("file") as File).name).toBe("attachment");
  });

  it("uploads an attachment to the load, with the latency as a query parameter", async () => {
    const { calls, api } = recorder(200, { attachment_id: "a1", document_id: "d1", event_id: "m1" });
    const result = await api.uploadAttachment(
      "481/207",
      { file: new File(["%PDF"], "pod_clean.pdf"), channel: "sms", senderContactId: "drv_20931", body: "", at: "t" },
      60,
    );
    expect(calls[0]?.url).toBe("/api/loads/481%2F207/attachments?latency_seconds=60");
    expect(calls[0]?.init?.method).toBe("POST");
    expect((formOf(calls[0]).get("file") as File).name).toBe("pod_clean.pdf");
    expect(result.document_id).toBe("d1");
  });

  it("uploads one SOP file per request under the broker", async () => {
    const { calls, api } = recorder(200, { document_id: "sd1", pages: 6, status: "compiled" });
    await api.uploadSop("acme", new File(["%PDF"], "acme_sop.pdf", { type: "application/pdf" }));
    expect(calls[0]?.url).toBe("/api/brokers/acme/sops");
    expect((formOf(calls[0]).get("file") as File).name).toBe("acme_sop.pdf");
  });
});

describe("brokers (card BRK)", () => {
  it("lists, reads and creates brokers", async () => {
    const { calls, api } = recorder(200, []);
    await api.listBrokers();
    await api.getBroker("acme west");
    await api.createBroker({ broker_id: "acme", name: "Acme Freight" });
    expect(calls.map((c) => `${c.init?.method} ${c.url}`)).toEqual([
      "GET /api/brokers",
      "GET /api/brokers/acme%20west",
      "POST /api/brokers",
    ]);
    expect(bodyOf(calls[2])).toEqual({ broker_id: "acme", name: "Acme Freight" });
  });

  it("puts only the editable fields: the API owns the versions and the rollout", async () => {
    const { calls, api } = recorder(200, {});
    const broker: Broker = { ...newBroker("acme", "Acme Freight"), config_version: 4, rollout: { percent: 50, previous_version: 3 } };
    await api.updateBroker("acme", brokerInput(broker));
    expect(calls[0]?.init?.method).toBe("PUT");
    expect(calls[0]?.url).toBe("/api/brokers/acme");
    const body = bodyOf(calls[0]);
    expect(Object.keys(body).sort()).toEqual(["document_types", "documents", "intents", "limits", "name", "obligations", "triage"]);
    expect(body.limits.max_steps).toBe(8);
  });

  it("reads the versions and sets the rollout", async () => {
    const { calls, api } = recorder(200, {});
    await api.brokerVersions("acme");
    await api.setRollout("acme", { percent: 0, previous_version: 1 });
    expect(calls[0]?.url).toBe("/api/brokers/acme/versions");
    expect(calls[1]?.url).toBe("/api/brokers/acme/rollout");
    expect(bodyOf(calls[1])).toEqual({ percent: 0, previous_version: 1 });
  });

  it("raises a 409 for an unknown previous version with the API's words", async () => {
    const { api } = recorder(409, { detail: "broker acme has no previous version 7" });
    await expect(api.setRollout("acme", { percent: 50, previous_version: 7 })).rejects.toMatchObject({ status: 409, detail: "broker acme has no previous version 7" });
  });
});

describe("knowledge (card KNW)", () => {
  it("reads a missing draft as null, and any other error as an error", async () => {
    expect(await recorder(404, { detail: "no draft" }).api.sopDraft("acme")).toBeNull();
    await expect(recorder(500, { detail: "boom" }).api.sopDraft("acme")).rejects.toBeInstanceOf(ApiError);
  });

  it("saves the reviewed draft and publishes it, as a canary when a percent is given", async () => {
    const { calls, api } = recorder(200, { version: "abc", config_version: 3, lint: [] });
    const draft: SopDraft = {
      broker_id: "acme",
      sections: [],
      proposed_intents: [],
      proposed_requirements: [],
      proposed_obligations: [],
      document_ids: ["sd1"],
      updated_at: "t",
    };
    await api.saveSopDraft("acme", draftInput({ ...draft, broker_id: "acme" }));
    await api.publishSop("acme", { percent: 20 });
    await api.publishSop("acme");
    await api.sopVersions("acme");
    expect(calls[0]?.init?.method).toBe("PUT");
    expect(calls[0]?.url).toBe("/api/brokers/acme/sops/draft");
    // SopDraftIn forbids extra fields: no broker_id, document_ids or updated_at.
    expect(bodyOf(calls[0])).toEqual({ sections: [], proposed_intents: [], proposed_requirements: [], proposed_obligations: [] });
    expect(calls[1]?.url).toBe("/api/brokers/acme/sops/publish");
    expect(bodyOf(calls[1])).toEqual({ percent: 20 });
    expect(bodyOf(calls[2])).toEqual({ percent: 100 });
    expect(calls[3]?.url).toBe("/api/brokers/acme/sops/versions");
  });

  it("searches the sections a broker's loads see", async () => {
    const { calls, api } = recorder(200, []);
    await api.searchSops({ brokerId: "keystone", loadTypes: ["reefer"], q: " trailer temperature seal ", k: 5 });
    expect(calls[0]?.url).toBe("/api/sops/search?q=trailer+temperature+seal&broker_id=keystone&load_types=reefer&k=5");
  });
});

describe("documents and broker evals (cards DOC, EVK)", () => {
  it("reads a load's documents and one document", async () => {
    const { calls, api } = recorder(200, []);
    await api.loadDocuments("730118");
    await api.getDocument("doc_pod_1");
    expect(calls[0]?.url).toBe("/api/loads/730118/documents");
    expect(calls[1]?.url).toBe("/api/documents/doc_pod_1");
  });

  it("runs a broker's suite with a cost cap and keeps the LangSmith link", async () => {
    const { calls, api } = recorder(200, {
      suite: "core",
      mode: "live",
      k: 1,
      langsmith_url: "https://smith.langchain.com/o/x/datasets/y/compare",
      scenarios: [{ scenario_id: "a", passed: true, failures: [], cost_usd: 0.002 }],
    });
    const summary = await api.runBrokerEvals("acme", { suite: "core", mode: "live", k: 1, cost_cap_usd: 0.1 });
    expect(calls[0]?.url).toBe("/api/brokers/acme/evals/run");
    expect(bodyOf(calls[0])).toEqual({ suite: "core", mode: "live", k: 1, cost_cap_usd: 0.1 });
    expect(summary.langsmithUrl).toBe("https://smith.langchain.com/o/x/datasets/y/compare");
    expect(summary.passed).toBe(1);
  });
});

describe("errorDetail", () => {
  it("joins FastAPI validation errors with their field path", () => {
    const payload = {
      detail: [
        { loc: ["body", "occurred_at"], msg: "Field required", type: "missing" },
        { loc: ["query", "latency_seconds"], msg: "Input should be a valid integer" },
      ],
    };
    expect(errorDetail(422, payload)).toBe(
      "occurred_at: Field required; query.latency_seconds: Input should be a valid integer",
    );
  });

  it("falls back to the status for an HTML error page", () => {
    expect(errorDetail(502, "<html>Bad gateway</html>")).toBe("Request failed with status 502");
  });
});

describe("runCost", () => {
  it("sums the usage of every model call", () => {
    const usage = (cost: number) => ({
      role: "decide",
      model: "m",
      input_tokens: 1,
      output_tokens: 1,
      cached_tokens: 0,
      cost_usd: cost,
      latency_ms: 1,
    });
    expect(runCost({ usage: [usage(0.0012), usage(0.0003)] })).toBeCloseTo(0.0015);
  });
});
