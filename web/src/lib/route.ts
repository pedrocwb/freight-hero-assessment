/**
 * Routes live in the hash, so a link to a load's run opens it: `#/load?load=481207&run=run_1`.
 * The old `#/inspector` links open the conversation too.
 */

export const SCREENS = ["load", "scenarios", "sops", "durable"] as const;
export type ScreenId = (typeof SCREENS)[number];
const DEFAULT_SCREEN: ScreenId = "load";
const ALIASES: Record<string, ScreenId> = { inspector: "load" };

export interface Route {
  screen: ScreenId;
  params: URLSearchParams;
}

export function parseHash(hash: string): Route {
  const [path = "", query = ""] = hash.replace(/^#\/?/, "").split("?");
  const named = ALIASES[path] ?? path;
  const screen = (SCREENS as readonly string[]).includes(named) ? (named as ScreenId) : DEFAULT_SCREEN;
  return { screen, params: new URLSearchParams(query) };
}

/** `#/load?load=481207&run=run_1`; empty values are left out. */
export function href(screen: ScreenId, params: Record<string, string | null | undefined> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const qs = query.toString();
  return `#/${screen}${qs ? `?${qs}` : ""}`;
}

/** The conversation of a load, opened on one of its runs. */
export function loadHref(loadId: string, runId?: string | null): string {
  return href("load", { load: loadId, run: runId });
}
