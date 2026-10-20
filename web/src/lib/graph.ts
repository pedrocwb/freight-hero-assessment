/**
 * The run graph (`GET /api/graph`, LangGraph's `draw_mermaid()`) with one run's path on it.
 *
 * The highlight is plain Mermaid appended to the source, so it is testable without a DOM:
 * the visited nodes get a `visited` class, a node visited more than once shows its count
 * in its label ("agent ×3"), and the edges the run took get a `linkStyle`. LangGraph's own
 * `classDef` lines are replaced by a palette that follows the page's light or dark theme.
 */

export const START_NODE = "__start__";
export const END_NODE = "__end__";

/** How many times the run entered each node, in the order of the first visit. */
export function visitCounts(path: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const node of path) counts.set(node, (counts.get(node) ?? 0) + 1);
  return counts;
}

export interface NodeStyle {
  fill: string;
  stroke: string;
  color: string;
}

export interface GraphPalette {
  node: NodeStyle;
  visited: NodeStyle;
  /** The ends (`__start__`, `__end__`) when the run did not reach them. */
  end: NodeStyle;
  /** The edges the run took. */
  taken: string;
}

// The page's tokens (styles.css): surfaces, ink and the guide-sign green.
export const LIGHT_PALETTE: GraphPalette = {
  node: { fill: "#f4f6f8", stroke: "#b4bfc7", color: "#41505a" },
  visited: { fill: "#dcefe6", stroke: "#0b6b4f", color: "#16222a" },
  end: { fill: "#e2e7eb", stroke: "#b4bfc7", color: "#66747e" },
  taken: "#0b6b4f",
};

export const DARK_PALETTE: GraphPalette = {
  node: { fill: "#273038", stroke: "#4b5862", color: "#b0bcc4" },
  visited: { fill: "#1d3a2f", stroke: "#44bf8e", color: "#e4eaee" },
  end: { fill: "#313c45", stroke: "#4b5862", color: "#8a98a2" },
  taken: "#44bf8e",
};

const style = (s: NodeStyle, extra = "") => `fill:${s.fill},stroke:${s.stroke},color:${s.color}${extra}`;

// `prefilter(prefilter)`, `__start__([<p>__start__</p>]):::first`, `tools["tools"]`.
const NODE_LINE = /^(\s*)([\w-]+)(\(\[|\[\[|\(\(|\(|\[|\{)(.*?)(\]\)|\]\]|\)\)|\)|\]|\})((?::::[\w-]+)*)(\s*;?\s*)$/;
// `a --> b;`, `a -.-> b;`, `a -. &nbsp;label&nbsp; .-> b;`, `a -- label --> b;`.
const EDGE_LINE = /^\s*([\w-]+)\s+(?:-\.->|--+>|==+>|-\..*?\.->|--\s.*?-->|==\s.*?==>)\s*([\w-]+)\s*;?\s*$/;

export interface Edge {
  /** The edge's index in definition order: the number `linkStyle` takes. */
  index: number;
  from: string;
  to: string;
}

export function parseEdges(mermaid: string): Edge[] {
  const edges: Edge[] = [];
  for (const line of mermaid.split("\n")) {
    const match = EDGE_LINE.exec(line);
    if (match) edges.push({ index: edges.length, from: match[1]!, to: match[2]! });
  }
  return edges;
}

export function parseNodes(mermaid: string): string[] {
  const nodes: string[] = [];
  for (const line of mermaid.split("\n")) {
    if (EDGE_LINE.test(line)) continue;
    const match = NODE_LINE.exec(line);
    if (match) nodes.push(match[2]!);
  }
  return nodes;
}

/** The label of a node shape without its quotes and markup: `<p>__start__</p>` reads `__start__`. */
function plainLabel(inner: string): string {
  const unquoted = /^"(.*)"$/.exec(inner.trim())?.[1] ?? inner.trim();
  return unquoted.replace(/<[^>]+>/g, "").trim();
}

export interface HighlightOptions {
  palette: GraphPalette;
  /** The run reached its end: the edge into `__end__` is part of its path. */
  finished: boolean;
}

/**
 * The Mermaid source with `path` drawn on it. Nodes of the path the graph does not define
 * are ignored, and an empty path leaves every node plain.
 */
export function highlightMermaid(mermaid: string, path: readonly string[], options: HighlightOptions): string {
  const defined = new Set(parseNodes(mermaid));
  const walked = path.filter((node) => defined.has(node));
  const counts = visitCounts(walked);
  if (walked.length && defined.has(START_NODE)) walked.unshift(START_NODE);
  if (walked.length && options.finished && defined.has(END_NODE)) walked.push(END_NODE);

  const lines: string[] = [];
  let indent = "\t";
  for (const line of mermaid.replace(/\r\n/g, "\n").split("\n")) {
    if (/^\s*classDef\s/.test(line)) continue; // LangGraph's palette, replaced below
    const node = EDGE_LINE.test(line) ? null : NODE_LINE.exec(line);
    if (node) indent = node[1] || indent;
    const visits = node ? counts.get(node[2]!) ?? 0 : 0;
    if (node && visits > 1) {
      const [, lead, id, open, inner, close, classes, tail] = node;
      lines.push(`${lead}${id}${open}"${plainLabel(inner!)} ×${visits}"${close}${classes}${tail}`);
    } else {
      lines.push(line);
    }
  }
  while (lines.length && !lines[lines.length - 1]!.trim()) lines.pop();

  const { palette } = options;
  lines.push(
    `${indent}classDef default ${style(palette.node)}`,
    `${indent}classDef first ${style(palette.end)}`,
    `${indent}classDef last ${style(palette.end)}`,
    `${indent}classDef visited ${style(palette.visited, ",stroke-width:2px")}`,
  );
  const visited = [...new Set(walked)];
  if (visited.length) lines.push(`${indent}class ${visited.join(",")} visited;`);

  const edges = parseEdges(mermaid);
  const taken = new Set<number>();
  for (let i = 0; i + 1 < walked.length; i += 1) {
    const edge = edges.find((e) => e.from === walked[i] && e.to === walked[i + 1]);
    if (edge) taken.add(edge.index);
  }
  if (taken.size) {
    const indices = [...taken].sort((a, b) => a - b).join(",");
    lines.push(`${indent}linkStyle ${indices} stroke:${palette.taken},stroke-width:2.5px;`);
  }
  return `${lines.join("\n")}\n`;
}

/** "prefilter, gate, knowledge, agent ×3, tools ×2, validate": the path in the order of first visits. */
export function describePath(path: readonly string[]): string {
  return [...visitCounts(path)].map(([node, n]) => (n > 1 ? `${node} ×${n}` : node)).join(", ");
}
