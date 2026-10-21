import { useEffect, useId, useState } from "react";

import { api, type RunRecord } from "../api";
import { useColorScheme } from "../hooks/useColorScheme";
import { useResource } from "../hooks/useResource";
import { DARK_PALETTE, highlightMermaid, LIGHT_PALETTE } from "../lib/graph";

type Mermaid = (typeof import("mermaid"))["default"];

// Mermaid is large, so it loads with the first graph shown, in its own chunk.
let mermaidModule: Promise<Mermaid> | null = null;
function loadMermaid(): Promise<Mermaid> {
  mermaidModule ??= import("mermaid").then((m) => m.default);
  return mermaidModule;
}

/**
 * The run graph (`GET /api/graph`) with the run's path on it: the nodes it visited, how
 * often, and the edges it took.
 */
export function RunGraph({ run }: { run: Pick<RunRecord, "nodes" | "finished_at"> }) {
  const graph = useResource("graph", () => api.graph(), []);
  const scheme = useColorScheme();
  const id = `run-graph-${useId().replace(/[^\w-]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const nodes = run.nodes ?? [];
  const pathKey = nodes.join(",");
  const finished = run.finished_at !== null;
  const source = graph.data?.mermaid;

  useEffect(() => {
    if (!source) return undefined;
    let cancelled = false;
    const text = highlightMermaid(source, pathKey ? pathKey.split(",") : [], {
      palette: scheme === "dark" ? DARK_PALETTE : LIGHT_PALETTE,
      finished,
    });
    void loadMermaid()
      .then(async (mermaid) => {
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: scheme === "dark" ? "dark" : "neutral" });
        const { svg: rendered } = await mermaid.render(id, text);
        if (!cancelled) {
          setSvg(rendered);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [source, pathKey, finished, scheme, id]);

  return (
    <figure className="run-graph">
      {graph.error ? (
        <p className="hint">The run graph is not available: {graph.error}</p>
      ) : error ? (
        <p className="hint">The graph could not be drawn: {error}</p>
      ) : svg ? (
        // Mermaid's own output, sanitized by it (securityLevel strict), from the API's graph.
        <div className="run-graph-svg" dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <p className="hint" aria-busy="true">
          Drawing the graph…
        </p>
      )}
    </figure>
  );
}
