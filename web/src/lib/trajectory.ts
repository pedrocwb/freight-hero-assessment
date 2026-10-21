/**
 * The agent loop's tool calls (`RunRecord.trajectory`), made readable for the run's
 * details: each call's arguments on one line, long texts clipped.
 */

/** The text cut to `max` characters, with an ellipsis when something was cut. */
export function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

function value(v: unknown, max: number): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return /[\s,=]/.test(v) || v === "" ? `"${clip(v, max)}"` : clip(v, max);
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map((item) => value(item, max)).join(", ")}]`;
  return clip(JSON.stringify(v), max);
}

/**
 * The arguments of a call on one line, `key=value` in the order the model gave them:
 * strings with spaces quoted and clipped, lists in brackets.
 */
export function compactArgs(args: Record<string, unknown> | null | undefined, max = 60): string {
  const entries = Object.entries(args ?? {});
  if (!entries.length) return "no arguments";
  return entries.map(([key, v]) => `${key}=${value(v, max)}`).join(", ");
}
