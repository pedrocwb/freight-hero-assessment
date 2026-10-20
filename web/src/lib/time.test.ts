import { describe, expect, it } from "vitest";

import {
  formatDateTime,
  formatDay,
  formatDuration,
  formatRelative,
  formatStamp,
  formatTime,
  formatWindow,
  groupByDay,
  minutesBetween,
} from "./time";

const LA = "America/Los_Angeles";

describe("formatTime", () => {
  it("shows the stop's local time with its abbreviation", () => {
    expect(formatTime("2026-10-13T14:31:00Z", LA)).toBe("07:31 PDT");
    expect(formatTime("2026-10-13T07:31:00-07:00", "America/Chicago")).toBe("09:31 CDT");
  });

  it("follows daylight saving time", () => {
    expect(formatTime("2026-12-01T16:00:00Z", LA)).toBe("08:00 PST");
  });

  it("falls back to UTC for an unknown zone and to dashes for no time", () => {
    expect(formatTime("2026-10-13T14:31:00Z", "Mars/Olympus")).toBe("14:31 UTC");
    expect(formatTime(null, LA)).toBe("--:--");
  });
});

describe("dates", () => {
  it("formats the local day and date with time", () => {
    expect(formatDay("2026-10-14T05:00:00Z", LA)).toBe("Tue, Oct 13");
    expect(formatDateTime("2026-10-14T09:30:00-07:00", LA)).toBe("Oct 14, 09:30 PDT");
  });

  it("drops the date when the instant is on the reference's local day", () => {
    const now = "2026-10-13T08:02:00-07:00";
    expect(formatStamp("2026-10-13T07:01:00-07:00", LA, now)).toBe("07:01 PDT");
    expect(formatStamp("2026-10-14T09:30:00-07:00", LA, now)).toBe("Oct 14, 09:30 PDT");
  });

  it("formats an appointment window", () => {
    expect(formatWindow("2026-10-13T06:00:00-07:00", "2026-10-13T14:00:00-07:00", LA)).toBe(
      "Oct 13, 06:00 to 14:00 PDT",
    );
  });
});

describe("durations", () => {
  it("measures real minutes across a DST change", () => {
    // The wall clock goes back from 01:55 PDT to 01:10 PST, but 15 real minutes pass.
    expect(minutesBetween("2026-11-01T01:55:00-07:00", "2026-11-01T01:10:00-08:00")).toBe(15);
  });

  it("reads like an operator would say it", () => {
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(250)).toBe("4 min 10 s");
    expect(formatDuration(7500)).toBe("2 h 05 min");
    expect(formatDuration(null)).toBe("n/a");
    expect(formatRelative(29)).toBe("in 29 min");
    expect(formatRelative(-3)).toBe("3 min ago");
    expect(formatRelative(0)).toBe("now");
  });
});

describe("groupByDay", () => {
  it("starts a group when the local day changes", () => {
    const groups = groupByDay(
      [
        { at: "2026-10-13T23:50:00-07:00" },
        { at: "2026-10-14T06:55:00Z" }, // still Oct 13 in Los Angeles
        { at: "2026-10-14T08:00:00-07:00" },
      ],
      LA,
    );
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ["Tue, Oct 13", 2],
      ["Wed, Oct 14", 1],
    ]);
  });
});
