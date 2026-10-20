import { describe, expect, it } from "vitest";

import { href, inspectorHref, parseHash } from "./route";

describe("routes", () => {
  it("reads the screen and its parameters from the hash", () => {
    const route = parseHash("#/inspector?load=481207&run=run_1");
    expect(route.screen).toBe("inspector");
    expect(route.params.get("load")).toBe("481207");
    expect(route.params.get("run")).toBe("run_1");
  });

  it("falls back to the scenarios for an unknown or empty hash", () => {
    expect(parseHash("").screen).toBe("scenarios");
    expect(parseHash("#/brokers").screen).toBe("scenarios");
  });

  it("builds links, leaving empty values out and escaping the rest", () => {
    expect(inspectorHref("481/207", "run_1")).toBe("#/inspector?load=481%2F207&run=run_1");
    expect(inspectorHref("481207")).toBe("#/inspector?load=481207");
    expect(href("sops")).toBe("#/sops");
    expect(parseHash(inspectorHref("481/207")).params.get("load")).toBe("481/207");
  });
});
