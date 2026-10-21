import { describe, expect, it } from "vitest";

import {
  ApiError,
  attachmentForm,
  buildRequest,
  buildUrl,
  createApi,
  errorDetail,
  inboundMessage,
  langsmithProject,
  modelReady,
  runCost,
  type Fetcher,
} from "./api";

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

  it("escapes the load id in its path", async () => {
    const { calls, api } = recorder(200, {});
    await api.getLoad("481/207");
    expect(calls[0]?.url).toBe("/api/loads/481%2F207");
  });

  it("builds the SOP preview query", async () => {
    const { calls, api } = recorder(200, []);
    await api.sopPreview({
      clientId: "keystone",
      loadTypes: ["reefer"],
      intent: "driver_departed",
      stage: "to_delivery",
    });
    expect(calls[0]?.url).toBe(
      "/api/sops/preview?client_id=keystone&load_types=reefer&intent=driver_departed&stage=to_delivery",
    );
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
    form.append("file", new Blob(["%PDF"], { type: "application/pdf" }), "pod_clean.pdf");
    const { init } = buildRequest("/api", { method: "POST", path: "/loads/481207/attachments", form });
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

});

describe("graph and scenarios", () => {
  it("reads the run graph and the scenarios", async () => {
    const { calls, api } = recorder(200, { mermaid: "graph TD;", nodes: [] });
    await api.graph();
    await api.scenarios();
    expect(calls.map((c) => `${c.init?.method} ${c.url}`)).toEqual(["GET /api/graph", "GET /api/scenarios"]);
  });

  it("starts a scenario by id, with no body", async () => {
    const { calls, api } = recorder(202, { scenario_id: "a b", load_id: "481207" });
    const started = await api.runScenario("a b");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(calls[0]?.url).toBe("/api/scenarios/a%20b/run");
    expect(calls[0]?.init?.body).toBeUndefined();
    expect(started.load_id).toBe("481207");
  });

  it("raises the 409 of a scenario already playing with the API's words", async () => {
    const { api } = recorder(409, { detail: "a scenario run is in progress" });
    await expect(api.runScenario("a")).rejects.toMatchObject({ status: 409, detail: "a scenario run is in progress" });
  });

  it("reads no scenario run yet, a 404 or an empty body, as null", async () => {
    expect(await recorder(404, { detail: "no scenario run yet" }).api.scenarioRun()).toBeNull();
    expect(await recorder(200, "").api.scenarioRun()).toBeNull();
    expect(await recorder(200, null).api.scenarioRun()).toBeNull();
    await expect(recorder(500, { detail: "boom" }).api.scenarioRun()).rejects.toMatchObject({ status: 500 });
  });

  it("resets the world with a start time only: there is no model mode", async () => {
    const { calls, api } = recorder(200, {});
    await api.resetSim({ start: "2026-10-13T06:55:00-07:00" });
    expect(bodyOf(calls[0])).toEqual({ start: "2026-10-13T06:55:00-07:00" });
  });
});

describe("model and LangSmith state", () => {
  it("is ready unless the API says false", () => {
    expect(modelReady({ model_ready: true })).toBe(true);
    expect(modelReady({ model_ready: false })).toBe(false);
    expect(modelReady(undefined)).toBe(true);
  });

  it("links the LangSmith project only when tracing is on", () => {
    const url = "https://smith.langchain.com/o/x/projects/p/y";
    expect(langsmithProject({ langsmith: { enabled: true, project_url: url } })).toBe(url);
    expect(langsmithProject({ langsmith: { enabled: false, project_url: url } })).toBeNull();
    expect(langsmithProject({ langsmith: { enabled: true, project_url: null } })).toBeNull();
    expect(langsmithProject(undefined)).toBeNull();
  });
});

describe("SOP search", () => {
  it("searches the sections a broker's loads see", async () => {
    const { calls, api } = recorder(200, []);
    await api.searchSops({ brokerId: "keystone", loadTypes: ["reefer"], q: " trailer temperature seal ", k: 5 });
    expect(calls[0]?.url).toBe("/api/sops/search?q=trailer+temperature+seal&broker_id=keystone&load_types=reefer&k=5");
  });
});

describe("documents", () => {
  it("reads a load's documents and its runs", async () => {
    const { calls, api } = recorder(200, []);
    await api.loadDocuments("730118");
    await api.runs("730118");
    expect(calls[0]?.url).toBe("/api/loads/730118/documents");
    expect(calls[1]?.url).toBe("/api/loads/730118/runs");
  });

  it("reads the facility notes of a load's stops", async () => {
    const { calls, api } = recorder(200, []);
    await api.facilityMemory("730118");
    expect(calls[0]?.url).toBe("/api/loads/730118/facility-memory");
    expect(calls[0]?.init?.method ?? "GET").toBe("GET");
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
