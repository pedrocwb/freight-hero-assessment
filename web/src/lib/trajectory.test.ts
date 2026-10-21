import { describe, expect, it } from "vitest";

import { clip, compactArgs } from "./trajectory";

describe("compactArgs", () => {
  it("writes key=value in the model's order, quoting strings with spaces", () => {
    expect(compactArgs({ contact_id: "drv_20931", text: "What time will you arrive?", purpose: "eta_request" })).toBe(
      'contact_id=drv_20931, text="What time will you arrive?", purpose=eta_request',
    );
  });

  it("puts lists in brackets and shows numbers, booleans and null as they are", () => {
    expect(compactArgs({ to_contact_ids: ["receiving@valleydc.example", "dsp_4410"], due_in_minutes: 20, urgent: false, subject: null })).toBe(
      "to_contact_ids=[receiving@valleydc.example, dsp_4410], due_in_minutes=20, urgent=false, subject=null",
    );
  });

  it("clips a long string and says when there is nothing", () => {
    expect(compactArgs({ body: "a".repeat(100) }, 10)).toBe(`body=${"a".repeat(9)}…`);
    expect(compactArgs({})).toBe("no arguments");
    expect(compactArgs(undefined)).toBe("no arguments");
  });
});

describe("clip", () => {
  it("flattens whitespace and cuts with an ellipsis", () => {
    expect(clip("one\n two   three", 40)).toBe("one two three");
    expect(clip("abcdefghij", 5)).toBe("abcd…");
  });
});
