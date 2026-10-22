/** Live workspace routes. Old load/inspector bookmarks open the durable view. */
export const SCREENS = ["scenarios", "sops", "durable"] as const;
export type ScreenId = (typeof SCREENS)[number];

export interface Route {
  screen: ScreenId;
  params: URLSearchParams;
}

export function parseHash(hash: string): Route {
  const [path = "", query = ""] = hash.replace(/^#\/?/, "").split("?");
  const params = new URLSearchParams(query);
  const screen = (SCREENS as readonly string[]).includes(path)
    ? (path as ScreenId)
    : params.get("load") && (path === "load" || path === "inspector")
      ? "durable"
      : "scenarios";
  return { screen, params };
}

export function href(screen: ScreenId, params: Record<string, string | null | undefined> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const qs = query.toString();
  return `#/${screen}${qs ? `?${qs}` : ""}`;
}
