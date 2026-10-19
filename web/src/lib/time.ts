/**
 * Time formatting. Every time on the page is shown in a stop's timezone with its
 * abbreviation ("07:31 PDT"), never in the browser's zone: the person playing the driver
 * and the SOP both reason in the local time of the stop.
 */

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`;
  let found = formatters.get(key);
  if (!found) {
    try {
      found = new Intl.DateTimeFormat("en-US", { ...options, timeZone });
    } catch {
      // An unknown zone name: show UTC rather than break the page.
      found = new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" });
    }
    formatters.set(key, found);
  }
  return found;
}

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function parts(date: Date, timeZone: string, options: Intl.DateTimeFormatOptions) {
  const out: Partial<Record<Intl.DateTimeFormatPartTypes, string>> = {};
  for (const part of formatter(timeZone, options).formatToParts(date)) out[part.type] = part.value;
  return out;
}

const TIME: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZoneName: "short",
};

/** "07:31" in the zone, without the abbreviation. */
export function clockTime(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "--:--";
  const p = parts(date, timeZone, TIME);
  return `${p.hour}:${p.minute}`;
}

/** The zone's abbreviation at that instant ("PDT" in summer, "PST" in winter). */
export function zoneAbbreviation(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso) ?? new Date(0);
  return parts(date, timeZone, TIME).timeZoneName ?? timeZone;
}

/** "07:31 PDT". */
export function formatTime(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "--:--";
  const p = parts(date, timeZone, TIME);
  return `${p.hour}:${p.minute} ${p.timeZoneName}`;
}

/** "07:31:20 PDT", for run timings. */
export function formatTimeSeconds(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "--:--:--";
  const p = parts(date, timeZone, { ...TIME, second: "2-digit" });
  return `${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}`;
}

/** "Tue, Oct 13". */
export function formatDay(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "";
  const p = parts(date, timeZone, { weekday: "short", month: "short", day: "numeric" });
  return `${p.weekday}, ${p.month} ${p.day}`;
}

/** "Oct 14, 09:30 PDT". */
export function formatDateTime(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "";
  const p = parts(date, timeZone, { ...TIME, month: "short", day: "numeric" });
  return `${p.month} ${p.day}, ${p.hour}:${p.minute} ${p.timeZoneName}`;
}

/** The local calendar day, for grouping and same-day checks. */
export function localDayKey(iso: string | null | undefined, timeZone: string): string {
  const date = parse(iso);
  if (!date) return "";
  const p = parts(date, timeZone, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${p.year}-${p.month}-${p.day}`;
}

/** Time only when it falls on the same local day as `reference`, otherwise date and time. */
export function formatStamp(
  iso: string | null | undefined,
  timeZone: string,
  reference: string | null | undefined,
): string {
  if (reference && localDayKey(iso, timeZone) === localDayKey(reference, timeZone)) {
    return formatTime(iso, timeZone);
  }
  return formatDateTime(iso, timeZone);
}

/** A fixed appointment, or a window: "Oct 13, 06:00 to 14:00 PDT". */
export function formatWindow(
  start: string | null | undefined,
  end: string | null | undefined,
  timeZone: string,
): string {
  if (!start || !end) return formatDateTime(start ?? end, timeZone);
  if (localDayKey(start, timeZone) === localDayKey(end, timeZone)) {
    const day = formatDateTime(start, timeZone).split(",")[0];
    return `${day}, ${clockTime(start, timeZone)} to ${formatTime(end, timeZone)}`;
  }
  return `${formatDateTime(start, timeZone)} to ${formatDateTime(end, timeZone)}`;
}

/** Signed minutes from `from` to `to` (real minutes, so DST does not skew them). */
export function minutesBetween(from: string, to: string): number {
  const a = parse(from);
  const b = parse(to);
  if (!a || !b) return 0;
  return Math.round((b.getTime() - a.getTime()) / 60000);
}

/** "45 s", "4 min 10 s", "2 h 05 min". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "n/a";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) {
    const rest = s % 60;
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
}

/** "in 29 min", "now", "3 min ago". */
export function formatRelative(minutes: number): string {
  if (minutes === 0) return "now";
  const span = formatDuration(Math.abs(minutes) * 60);
  return minutes > 0 ? `in ${span}` : `${span} ago`;
}

/** Shift an ISO instant by whole minutes, keeping the offset form the API understands. */
export function addMinutes(iso: string, minutes: number): string {
  const date = parse(iso);
  if (!date) return iso;
  return new Date(date.getTime() + minutes * 60000).toISOString();
}
