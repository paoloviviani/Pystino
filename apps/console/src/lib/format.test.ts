import { afterEach, describe, expect, it, vi } from "vitest";
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatRelative,
  formatShortDate,
} from "./format";

/**
 * The formatters exist so that every screen prints a date or a count the same
 * way, instead of each growing its own `toLocale*` call.
 *
 * What is pinned here is the decision they currently carry: the locale is the
 * browser's, which is what the screens did before they had these helpers, and
 * not the catalogue's. That is a choice that will be reversed when a second
 * language exists, and it is invisible on a machine whose browser already says
 * English — so the last test asserts what was passed to `Intl` rather than what
 * came out.
 */

// Local noon, so that no timezone on a CI machine can move the date.
const DAY = new Date(2026, 9, 16, 12);
const DATE_STYLE = { year: "numeric", month: "short", day: "numeric" } as const;
const SHORT_DATE_STYLE = { month: "short", day: "numeric" } as const;
const DATE_TIME_STYLE = {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
} as const;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("dates", () => {
  it("formats a date the way the browser's locale writes it", () => {
    expect(formatDate(DAY)).toBe(new Intl.DateTimeFormat(undefined, DATE_STYLE).format(DAY));
  });

  it("drops the year for a figure known to be recent", () => {
    expect(formatShortDate(DAY)).toBe(new Intl.DateTimeFormat(undefined, SHORT_DATE_STYLE).format(DAY));
  });

  it("adds the time of day to the minute", () => {
    expect(formatDateTime(DAY)).toBe(new Intl.DateTimeFormat(undefined, DATE_TIME_STYLE).format(DAY));
  });

  it("accepts the ISO string the API sends", () => {
    expect(formatDate(new Date(2026, 9, 16, 12).toISOString())).toBe(formatDate(DAY));
  });
});

describe("numbers", () => {
  it("groups digits the way the browser's locale does", () => {
    expect(formatNumber(1_234_567)).toBe(new Intl.NumberFormat(undefined).format(1_234_567));
  });
});

describe("durations", () => {
  it("pluralizes the unit itself", () => {
    // The console used to write this as `value === 1 ? "hour" : `${value}
    // hours`` in more than one screen, which is a rule about English.
    const long = { style: "unit", unit: "hour", unitDisplay: "long" } as const;
    expect(formatDuration(1, "hour")).toBe(new Intl.NumberFormat(undefined, long).format(1));
    expect(formatDuration(6, "hour")).toBe(new Intl.NumberFormat(undefined, long).format(6));
  });
});

describe("relative time", () => {
  const now = new Date(2026, 9, 16, 12);

  it("uses the coarsest unit that still says something", () => {
    // The unit boundaries are the console's decision; the wording is the
    // browser's, so it is compared rather than spelled out.
    const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    expect(formatRelative(new Date(2026, 9, 13, 12), now)).toBe(relative.format(-3, "day"));
    expect(formatRelative(new Date(2026, 9, 16, 11), now)).toBe(relative.format(-1, "hour"));
  });

  it("counts seconds rather than rounding to now", () => {
    const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    expect(formatRelative(new Date(2026, 9, 16, 11, 59, 50), now)).toBe(relative.format(-10, "second"));
  });

  it("says what is ahead of the reader as well as what is behind", () => {
    const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    expect(formatRelative(new Date(2026, 9, 18, 12), now)).toBe(relative.format(2, "day"));
  });
});

describe("the locale it is given", () => {
  it("is the browser's, not the catalogue's, and not \"en\" either", async () => {
    // Everything above compares against an `Intl` call with `undefined`, which
    // on a machine whose browser already says English cannot tell `undefined`
    // from `"en"`. This one watches the constructor instead.
    vi.resetModules();
    const seen: unknown[] = [];
    const realDates = Intl.DateTimeFormat;
    const realNumbers = Intl.NumberFormat;
    vi.stubGlobal("Intl", {
      ...Intl,
      // `function`, not an arrow: the console calls these with `new`.
      DateTimeFormat: function (locale?: string | string[], options?: Intl.DateTimeFormatOptions) {
        seen.push(locale);
        return new realDates(locale, options);
      },
      NumberFormat: function (locale?: string | string[], options?: Intl.NumberFormatOptions) {
        seen.push(locale);
        return new realNumbers(locale, options);
      },
    });
    const fresh = await import("./format");
    fresh.formatDate(DAY);
    fresh.formatNumber(1_234);
    expect(seen).toEqual([undefined, undefined]);
  });
});
