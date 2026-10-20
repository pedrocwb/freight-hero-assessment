import { describe, expect, it } from "vitest";

import type { DraftSection, ProposedIntent, SopDraft } from "../api";
import { decideIntent, draftProblems, evidenceLines, fileNames, knownIntents, pagesLabel, requirementLabel, sourceLabel, undecided } from "./sopDraft";

const section = (id: string, intents: string[], more: Partial<DraftSection> = {}): DraftSection => ({
  section_id: id,
  title: humanTitle(id),
  description: "",
  intents,
  stages: [],
  mode: "replace",
  body: "Do the thing.",
  source: { document_id: "sd2", pages: [3] },
  proposed: true,
  ...more,
});

function humanTitle(id: string): string {
  return id.replace(/_/g, " ");
}

const detention: ProposedIntent = {
  intent_id: "detention_request",
  title: "Detention request",
  description: "Waiting beyond the free time.",
  examples: [],
  priority: 35,
  must_act: true,
  noop_eligible: false,
  system: false,
  evidence: 'Page 3: "open a detention request"',
  accepted: null,
};

function draft(sections: DraftSection[], accepted: boolean | null = null): SopDraft {
  return {
    broker_id: "acme",
    sections,
    proposed_intents: [{ ...detention, accepted }],
    proposed_requirements: [{ type_id: "pod", at_stage: "at_delivery", within_minutes: 120, opens_obligation: "pod" }],
    proposed_obligations: [],
    document_ids: ["sd2"],
    updated_at: "t",
  };
}

const BROKER = ["delay_risk_high", "eta_follow_up_due", "question", "document"];

describe("labels", () => {
  it("writes pages as ranges", () => {
    expect(pagesLabel([])).toBe("");
    expect(pagesLabel([3])).toBe("page 3");
    expect(pagesLabel([4, 2, 3])).toBe("pages 2–4");
    expect(pagesLabel([1, 3, 4, 7])).toBe("pages 1, 3–4, 7");
  });

  it("names the source by file name when the document is known", () => {
    const docs = [{ document_id: "sd2", broker_id: "acme", filename: "acme_sop.pdf", content_type: "application/pdf", pages: 6, sha256: "x", uploaded_at: "t" }];
    const names = fileNames(docs, { sd1: "notes.md", sd2: "stale name.pdf" });
    expect(names).toEqual({ sd1: "notes.md", sd2: "acme_sop.pdf" });
    expect(sourceLabel({ document_id: "sd2", pages: [3, 4] }, names)).toBe("acme_sop.pdf, pages 3–4");
    expect(sourceLabel({ document_id: "sd9", pages: [] }, names)).toBe("sd9");
    expect(sourceLabel(null)).toBe("written by hand");
  });

  it("reads the evidence as quoted lines, one per line of the string", () => {
    expect(evidenceLines("  one quote \n\n another ")).toEqual(["one quote", "another"]);
    expect(evidenceLines(["a", " ", "b"])).toEqual(["a", "b"]);
    expect(evidenceLines("")).toEqual([]);
  });

  it("writes a requirement in one line", () => {
    expect(requirementLabel({ type_id: "pod", at_stage: "at_delivery", within_minutes: 120 })).toBe("pod at at_delivery, within 120 min");
  });
});

describe("decisions on proposed intents", () => {
  it("records a decision without touching the rest", () => {
    const before = draft([section("detention_request", ["detention_request"])]);
    const after = decideIntent(before, "detention_request", true);
    expect(after.proposed_intents[0]?.accepted).toBe(true);
    expect(before.proposed_intents[0]?.accepted).toBeNull();
    expect(undecided(before)).toBe(1);
    expect(undecided(after)).toBe(0);
  });

  it("adds only accepted proposals to the known intents", () => {
    expect(knownIntents(BROKER, draft([], null)).has("detention_request")).toBe(false);
    expect(knownIntents(BROKER, draft([], true)).has("detention_request")).toBe(true);
  });
});

describe("draftProblems", () => {
  const sections = [section("delay_risk_high", ["delay_risk_high", "eta_follow_up_due"]), section("detention_request", ["detention_request"])];

  it("asks for a decision on a proposed intent a section applies to", () => {
    expect(draftProblems(draft(sections, null), BROKER)).toEqual([
      "Section detention_request applies to detention_request, a proposed intent: accept it or remove it from the section.",
    ]);
  });

  it("passes once the intent is accepted", () => {
    expect(draftProblems(draft(sections, true), BROKER, ["pod", "bol"])).toEqual([]);
  });

  it("says when a rejected intent is still applied, or an intent is unknown", () => {
    const withUnknown = [...sections, section("lumper", ["lumper_receipt"])];
    expect(draftProblems(draft(withUnknown, false), BROKER)).toEqual([
      "Section detention_request applies to detention_request, which was rejected: remove it from the section's intents.",
      "Section lumper applies to lumper_receipt, which is not an intent of this broker.",
    ]);
  });

  it("lets a section that extends the standard one keep the standard title", () => {
    const extend = section("documents", [], { title: "", mode: "extend" });
    expect(draftProblems(draft([...sections, extend], true), BROKER)).toEqual([]);
  });

  it("refuses an accepted intent that no section applies to, a twice-listed section and an empty one", () => {
    const list = [section("delay_risk_high", ["delay_risk_high"]), section("delay_risk_high", [], { title: "", body: " " })];
    expect(draftProblems(draft(list, true), BROKER)).toEqual([
      "Section delay_risk_high appears twice.",
      "Section delay_risk_high has no title.",
      "Section delay_risk_high has no text.",
      "Intent detention_request is accepted but no section applies to it.",
    ]);
  });

  it("checks the proposed requirements against the broker's document types when given", () => {
    expect(draftProblems(draft(sections, true), BROKER, ["bol"])).toEqual([
      "Requirement pod at at_delivery, within 120 min: unknown document type pod.",
    ]);
  });

  it("needs the obligation a requirement opens, from the broker or proposed with the draft", () => {
    const d = draft(sections, true);
    expect(draftProblems(d, BROKER, undefined, ["eta_request"])).toEqual([
      "Requirement pod at at_delivery, within 120 min opens pod, which is neither an obligation of the broker nor a proposed one.",
    ]);
    const pod = { kind: "pod", title: "POD request", description: "", follow_up_minutes: 120, max_attempts: 1, follow_up_intent: "", on_exhausted: ["hero_task" as const] };
    expect(draftProblems({ ...d, proposed_obligations: [pod] }, BROKER, undefined, ["eta_request"])).toEqual([]);
  });
});
