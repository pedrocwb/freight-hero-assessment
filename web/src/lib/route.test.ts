import { describe, expect, it } from "vitest";

import { href, parseHash } from "./route";

describe("routes", () => {
  it("reads the screen and its parameters from the hash", () => {
    const route = parseHash("#/load?load=481207&run=run_1");
    expect(route.screen).toBe("durable");
    expect(route.params.get("load")).toBe("481207");
    expect(route.params.get("run")).toBe("run_1");
  });

  it("opens scenarios by default and redirects old load bookmarks to live operations", () => {
    expect(parseHash("").screen).toBe("scenarios");
    expect(parseHash("#/brokers").screen).toBe("scenarios");
    expect(parseHash("#/inspector?load=481207").screen).toBe("durable");
  });

  it("builds links, leaving empty values out and escaping the rest", () => {
    expect(href("durable", { load: "481/207", broker: "keystone" })).toBe("#/durable?load=481%2F207&broker=keystone");
    expect(href("durable", { load: "481207" })).toBe("#/durable?load=481207");
    expect(href("sops")).toBe("#/sops");
    expect(parseHash(href("durable", { load: "481/207" })).params.get("load")).toBe("481/207");
  });
});
