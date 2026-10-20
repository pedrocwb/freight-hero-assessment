import { describe, expect, it } from "vitest";

import { describePath, highlightMermaid, LIGHT_PALETTE, parseEdges, parseNodes, visitCounts } from "./graph";
import { GRAPH } from "./testFixtures";

const LOOP = ["prefilter", "gate", "knowledge", "agent", "tools", "agent", "validate"];

const linesOf = (text: string) => text.split("\n").map((l) => l.trim());

describe("parsing LangGraph's Mermaid", () => {
  it("finds the nodes and the edges in definition order", () => {
    expect(parseNodes(GRAPH.mermaid)).toEqual(GRAPH.nodes);
    const edges = parseEdges(GRAPH.mermaid);
    expect(edges).toHaveLength(10);
    expect(edges[0]).toEqual({ index: 0, from: "__start__", to: "prefilter" });
    expect(edges[8]).toEqual({ index: 8, from: "tools", to: "agent" });
  });

  it("reads labelled conditional edges", () => {
    const text = "graph TD;\n\tgate -. &nbsp;act&nbsp; .-> knowledge;\n\tgate -- skip --> __end__;\n";
    expect(parseEdges(text).map((e) => `${e.from}>${e.to}`)).toEqual(["gate>knowledge", "gate>__end__"]);
  });
});

describe("visitCounts", () => {
  it("counts each node's visits in the order of the first one", () => {
    expect([...visitCounts(LOOP)]).toEqual([
      ["prefilter", 1],
      ["gate", 1],
      ["knowledge", 1],
      ["agent", 2],
      ["tools", 1],
      ["validate", 1],
    ]);
    expect(describePath(LOOP)).toBe("prefilter, gate, knowledge, agent ×2, tools, validate");
  });
});

describe("highlightMermaid", () => {
  const text = highlightMermaid(GRAPH.mermaid, LOOP, { palette: LIGHT_PALETTE, finished: true });
  const lines = linesOf(text);

  it("marks every node of the path, the ends included, as visited", () => {
    expect(lines).toContain("class __start__,prefilter,gate,knowledge,agent,tools,validate,__end__ visited;");
  });

  it("shows the visit count of a node entered more than once", () => {
    expect(lines).toContain('agent("agent ×2")');
    expect(lines).toContain("tools(tools)");
  });

  it("styles the edges the run took, by their index", () => {
    // start>prefilter 0, agent>tools 1, agent>validate 2, gate>knowledge 4, knowledge>agent 5,
    // prefilter>gate 7, tools>agent 8, validate>end 9.
    expect(lines).toContain(`linkStyle 0,1,2,4,5,7,8,9 stroke:${LIGHT_PALETTE.taken},stroke-width:2.5px;`);
  });

  it("replaces LangGraph's palette with the page's and keeps the rest of the source", () => {
    expect(text).not.toContain("#f2f0ff");
    expect(lines.filter((l) => l.startsWith("classDef")).map((l) => l.split(" ")[1])).toEqual(["default", "first", "last", "visited"]);
    expect(text.startsWith("---\nconfig:\n  flowchart:\n    curve: linear\n---\ngraph TD;\n")).toBe(true);
    expect(lines).toContain("__start__([<p>__start__</p>]):::first");
  });

  it("stops at the last node of a run that has not finished", () => {
    const partial = linesOf(highlightMermaid(GRAPH.mermaid, ["prefilter", "gate"], { palette: LIGHT_PALETTE, finished: false }));
    expect(partial).toContain("class __start__,prefilter,gate visited;");
    expect(partial).toContain(`linkStyle 0,7 stroke:${LIGHT_PALETTE.taken},stroke-width:2.5px;`);
  });

  it("takes the prefilter's way out for a skipped run", () => {
    const skipped = linesOf(highlightMermaid(GRAPH.mermaid, ["prefilter"], { palette: LIGHT_PALETTE, finished: true }));
    expect(skipped).toContain(`linkStyle 0,6 stroke:${LIGHT_PALETTE.taken},stroke-width:2.5px;`);
  });

  it("ignores nodes the graph does not define, and leaves an empty path plain", () => {
    const unknown = highlightMermaid(GRAPH.mermaid, ["prefilter", "ghost"], { palette: LIGHT_PALETTE, finished: false });
    expect(unknown).toContain("class __start__,prefilter visited;");
    expect(unknown).not.toContain("ghost");
    const plain = highlightMermaid(GRAPH.mermaid, [], { palette: LIGHT_PALETTE, finished: true });
    expect(plain).not.toContain(" visited;");
    expect(plain).not.toContain("linkStyle");
  });
});
