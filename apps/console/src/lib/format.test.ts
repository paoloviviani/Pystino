import { describe, expect, it } from "vitest";
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatRelative,
  formatShortDate,
} from "./format";

/**
 * The formatters exist so that the shape of a figure follows the language the
 * console is in, not the language of the browser. What is pinned here is
 * therefore two things: that each one is an `Intl` call on the base locale
 * (compared against the same call made directly, which is the only honest way
 * to say "English formatting"), and that the plural inside a duration is the
 * formatter's business rather than a branch in a screen.
 */

// Local noon, so that no timezone on a CI machine can move the date.
const DAY = new Date(2026, 9, 16, 12);

describe("dates", () => {
  it("formats a date in the console's locale, not the browser's", () => {
    expect(formatDate(DAY)).toBe(
      new Intl.DateTimeFormat("en", { year: "numeric", month: "short", day: "numeric" }).format(DAY),
    );
    expect(formatDate(DAY)).toBe("Oct 16, 2026");
  });

  it("drops the year for a figure known to be recent", () => {
    expect(formatShortDate(DAY)).toBe("Oct 16");
  });

  it("adds the time of day to the minute", () => {
    // The exact glyphs are the platform's: 12:00 and 12:00 PM are both correct,
    // and which one appears is not the console's decision to make.
    expect(formatDateTime(DAY)).toMatch(/Oct 16, 2026.*12:00/);
  });

  it("accepts the ISO string the API sends", () => {
    expect(formatDate(new Date(2026, 9, 16, 12).toISOString())).toBe("Oct 16, 2026");
  });
});

describe("numbers", () => {
  it("groups digits the way the active locale does", () => {
    expect(formatNumber(1_234_567)).toBe("1,234,567");
  });
});

describe("durations", () => {
  it("pluralizes the unit itself", () => {
    // The console used to write this as `value === 1 ? "hour" : `${value}
    // hours`` in more than one screen, which is a rule about English.
    expect(formatDuration(1, "hour")).toBe("1 hour");
    expect(formatDuration(6, "hour")).toBe("6 hours");
  });
});

describe("relative time", () => {
  const now = new Date(2026, 9, 16, 12);

  it("uses the coarsest unit that still says something", () => {
    expect(formatRelative(new Date(2026, 9, 13, 12), now)).toBe("3 days ago");
    expect(formatRelative(new Date(2026, 9, 9, 12), now)).toBe("last week");
    expect(formatRelative(new Date(2026, 9, 16, 11), now)).toBe("1 hour ago");
  });

  it("counts seconds rather than rounding to now", () => {
    expect(formatRelative(new Date(2026, 9, 16, 11, 59, 50), now)).toBe("10 seconds ago");
  });

  it("says what is ahead of the reader as well as what is behind", () => {
    expect(formatRelative(new Date(2026, 9, 18, 12), now)).toBe("in 2 days");
  });
});
