/**
 * Reviewing a compiled SOP draft (card KNW). `draftProblems` mirrors the publish lint the
 * API runs (every section applies to known intents, every accepted intent has a section),
 * so a person sees what blocks a publish while editing; the API's lint stays the verdict.
 */

import type { DocumentRequirement, SopDocumentView, SopDraft, SourceRef } from "../api";

/** File names by document id: the draft's documents when the API sends them, then the page's own uploads. */
export function fileNames(
  documents: readonly SopDocumentView[] | undefined,
  uploaded: Readonly<Record<string, string>> = {},
): Record<string, string> {
  const names: Record<string, string> = { ...uploaded };
  for (const d of documents ?? []) names[d.document_id] = d.filename;
  return names;
}

/** [3] reads "page 3"; [2, 3, 4] "pages 2–4"; [1, 3] "pages 1, 3". */
export function pagesLabel(pages: readonly number[]): string {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  if (!sorted.length) return "";
  if (sorted.length === 1) return `page ${sorted[0]}`;
  const runs: string[] = [];
  let start = sorted[0]!;
  let end = start;
  for (const page of sorted.slice(1)) {
    if (page === end + 1) {
      end = page;
      continue;
    }
    runs.push(start === end ? `${start}` : `${start}–${end}`);
    start = end = page;
  }
  runs.push(start === end ? `${start}` : `${start}–${end}`);
  return `pages ${runs.join(", ")}`;
}

/** "acme_sop.pdf, pages 2–3"; the document id when the file name is not known. */
export function sourceLabel(source: SourceRef | null | undefined, names: Readonly<Record<string, string>> = {}): string {
  if (!source) return "written by hand";
  const pages = pagesLabel(source.pages);
  return [names[source.document_id] ?? source.document_id, pages].filter(Boolean).join(", ");
}

/** The compiler's evidence for a proposed intent (one quote per line), as quoted lines. */
export function evidenceLines(evidence: string | readonly string[] | null | undefined): string[] {
  if (!evidence) return [];
  const list = typeof evidence === "string" ? evidence.split("\n") : [...evidence];
  return list.map((line) => line.trim()).filter(Boolean);
}

export function decideIntent<T extends SopDraft>(draft: T, intentId: string, accepted: boolean | null): T {
  return {
    ...draft,
    proposed_intents: draft.proposed_intents.map((p) => (p.intent_id === intentId ? { ...p, accepted } : p)),
  };
}

/** The intents a published draft may apply to: the broker's plus the accepted proposals. */
export function knownIntents(brokerIntentIds: Iterable<string>, draft: Pick<SopDraft, "proposed_intents">): Set<string> {
  const known = new Set(brokerIntentIds);
  for (const p of draft.proposed_intents) if (p.accepted === true) known.add(p.intent_id);
  return known;
}

export function requirementLabel(r: Pick<DocumentRequirement, "type_id" | "at_stage" | "within_minutes">): string {
  const within = r.within_minutes ? `, within ${r.within_minutes} min` : "";
  return `${r.type_id} at ${r.at_stage}${within}`;
}

/**
 * What stops this draft from publishing, in plain words; empty when it is ready.
 * `documentTypes` (the broker's type ids) and `obligationKinds` (its obligation kinds)
 * are checked only when given; a proposed obligation counts as known.
 */
export function draftProblems(
  draft: SopDraft,
  brokerIntentIds: Iterable<string>,
  documentTypes?: Iterable<string>,
  obligationKinds?: Iterable<string>,
): string[] {
  const problems: string[] = [];
  const known = knownIntents(brokerIntentIds, draft);
  const seen = new Set<string>();
  for (const section of draft.sections) {
    if (seen.has(section.section_id)) problems.push(`Section ${section.section_id} appears twice.`);
    seen.add(section.section_id);
    // A section that extends the standard one keeps its title; one that replaces it needs its own.
    if (!section.title.trim() && section.mode !== "extend") problems.push(`Section ${section.section_id} has no title.`);
    if (!section.body.trim()) problems.push(`Section ${section.section_id} has no text.`);
    for (const intent of section.intents) {
      if (known.has(intent)) continue;
      const proposal = draft.proposed_intents.find((p) => p.intent_id === intent);
      problems.push(
        proposal
          ? proposal.accepted === false
            ? `Section ${section.section_id} applies to ${intent}, which was rejected: remove it from the section's intents.`
            : `Section ${section.section_id} applies to ${intent}, a proposed intent: accept it or remove it from the section.`
          : `Section ${section.section_id} applies to ${intent}, which is not an intent of this broker.`,
      );
    }
  }
  for (const p of draft.proposed_intents) {
    if (p.accepted !== true) continue;
    if (!draft.sections.some((s) => s.intents.includes(p.intent_id))) {
      problems.push(`Intent ${p.intent_id} is accepted but no section applies to it.`);
    }
  }
  if (documentTypes) {
    const types = new Set(documentTypes);
    for (const r of draft.proposed_requirements) {
      if (!types.has(r.type_id)) problems.push(`Requirement ${requirementLabel(r)}: unknown document type ${r.type_id}.`);
    }
  }
  if (obligationKinds) {
    const kinds = new Set(obligationKinds);
    for (const o of draft.proposed_obligations ?? []) kinds.add(o.kind);
    for (const r of draft.proposed_requirements) {
      if (r.opens_obligation && !kinds.has(r.opens_obligation)) {
        problems.push(`Requirement ${requirementLabel(r)} opens ${r.opens_obligation}, which is neither an obligation of the broker nor a proposed one.`);
      }
    }
  }
  return problems;
}

/** Proposed intents still waiting for a person. */
export function undecided(draft: Pick<SopDraft, "proposed_intents">): number {
  return draft.proposed_intents.filter((p) => p.accepted === null).length;
}
