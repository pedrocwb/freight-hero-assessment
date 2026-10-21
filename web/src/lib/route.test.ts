import { describe, expect, it } from "vitest";

import { href, loadHref, parseHash } from "./route";

describe("routes", () => {
  it("reads the screen and its parameters from the hash", () => {
    const route = parseHash("#/load?load=481207&run=run_1");
    expect(route.screen).toBe("load");
    expect(route.params.get("load")).toBe("481207");
    expect(route.params.get("run")).toBe("run_1");
  });

  it("opens the conversation for an unknown or empty hash, and for an old inspector link", () => {
    expect(parseHash("").screen).toBe("load");
    expect(parseHash("#/brokers").screen).toBe("load");
    expect(parseHash("#/inspector?load=481207").screen).toBe("load");
  });

  it("builds links, leaving empty values out and escaping the rest", () => {
    expect(loadHref("481/207", "run_1")).toBe("#/load?load=481%2F207&run=run_1");
    expect(loadHref("481207")).toBe("#/load?load=481207");
    expect(href("sops")).toBe("#/sops");
    expect(parseHash(loadHref("481/207")).params.get("load")).toBe("481/207");
  });
});
