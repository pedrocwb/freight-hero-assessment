import { describe, expect, it } from "vitest";

import {
  ApiError,
  buildRequest,
  buildUrl,
  createApi,
  errorDetail,
  inboundMessage,
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
