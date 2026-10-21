/**
 * Routes live in the hash, so a link to a load's run opens it: `#/inspector?load=481207&run=run_1`.
 */

export const SCREENS = ["scenarios", "inspector", "sops"] as const;
export type ScreenId = (typeof SCREENS)[number];
const DEFAULT_SCREEN: ScreenId = "scenarios";

export interface Route {
  screen: ScreenId;
  params: URLSearchParams;
}

export function parseHash(hash: string): Route {
  const [path = "", query = ""] = hash.replace(/^#\/?/, "").split("?");
  const screen = (SCREENS as readonly string[]).includes(path) ? (path as ScreenId) : DEFAULT_SCREEN;
  return { screen, params: new URLSearchParams(query) };
}

/** `#/inspector?load=481207&run=run_1`; empty values are left out. */
export function href(screen: ScreenId, params: Record<string, string | null | undefined> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const qs = query.toString();
  return `#/${screen}${qs ? `?${qs}` : ""}`;
}

/** The inspector on a load, opened at one of its runs. */
export function inspectorHref(loadId: string, runId?: string | null): string {
  return href("inspector", { load: loadId, run: runId });
}
