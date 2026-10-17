/**
 * Every date, number and duration the console shows, in one place.
 *
 * The locale here is `undefined`, which is what every screen already passed to
 * `toLocaleString()` and `toLocaleDateString()`: the browser's own language.
 * That is deliberate for now, and it is the one thing here that will change
 * with a translation. English is the only catalogue the console ships, so
 * switching these to the catalogue's locale would change what a reader sees
 * today — a reader whose browser says German gets German date formats in an
 * English console, and would suddenly get English ones. Nobody asked for that
 * as part of adding a message catalogue, and a change nobody asked for is
 * still a change.
 *
 * **When a second language exists, pass `getLocale()` from lib/locale instead
 * of `undefined`, and update the three tests that compare against an `Intl`
 * call.** Then the language of the words and the shape of the figures are
 * decided together, and the helper's callers do not change.
 *
 * None of them take a locale argument. A formatter that can be handed the
 * wrong locale is a formatter that will be, the first time somebody needs
 * "the reader's locale" for one field and "the console's" for the next.
 *
 * Money is not here at all: an amount stays a string from the API to
 * `formatMoney` in packages/ui, and nothing in the console may parse one.
 */

type Primitive = string | number | Date;

/**
 * `Intl` constructors are not free and a screen re-renders a lot, so each one
 * is built once per style. The locale is not in the key because it is not ours
 * to name: it is the browser's, and it cannot change under a page that has
 * already loaded. When it becomes `getLocale()`, add it back to the key — a
 * locale the reader can change without a reload is exactly the case a cache
 * keyed without it gets wrong.
 *
 * `BROWSER` stands in for that unnamed locale as the one key.
 */
const BROWSER = "browser";

const dates = new Map<string, Intl.DateTimeFormat>();
const numbers = new Map<string, Intl.NumberFormat>();
const relatives = new Map<string, Intl.RelativeTimeFormat>();

function dateFormatter(style: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify(style);
  const built = dates.get(key) ?? new Intl.DateTimeFormat(undefined, style);
  dates.set(key, built);
  return built;
}

function numberFormatter(style?: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = JSON.stringify(style);
  const built = numbers.get(key) ?? new Intl.NumberFormat(undefined, style);
  numbers.set(key, built);
  return built;
}

function relativeFormatter(): Intl.RelativeTimeFormat {
  const built = relatives.get(BROWSER) ?? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  relatives.set(BROWSER, built);
  return built;
}

/** The date every table and detail panel has always shown. */
const DATE_STYLE: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", day: "numeric" };

/** The same date without the year, for a figure known to be recent. */
const SHORT_DATE_STYLE: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };

const DATE_TIME_STYLE: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
};

/** The API sends timestamps as ISO strings; nothing in the console should parse one by hand. */
function toDate(value: Primitive): Date {
  return value instanceof Date ? value : new Date(value);
}

function toNumber(value: Primitive): number {
  return value instanceof Date ? value.getTime() : Number(value);
}

/**
 * A date, the way the tables and detail panels have always shown one —
 * `Oct 16, 2026` for a reader in the United States, `16 Oct 2026` for one in
 * Italy. The style is not a preference, it is what every existing screen
 * prints, and a column of mixed date formats is harder to read than a
 * uniform one.
 */
export function formatDate(value: Primitive): string {
  return dateFormatter(DATE_STYLE).format(toDate(value));
}

/**
 * A date without the year, for a figure known to be recent — a quota's reset,
 * a token's expiry inside the period a reader is looking at. The year is
 * dropped only where its absence cannot mislead.
 */
export function formatShortDate(value: Primitive): string {
  return dateFormatter(SHORT_DATE_STYLE).format(toDate(value));
}

/** A date and the time of day, to the minute: the default across the console. */
export function formatDateTime(value: Primitive): string {
  return dateFormatter(DATE_TIME_STYLE).format(toDate(value));
}

/**
 * A count, grouped the way the reader's browser groups: `1,234,567` in
 * English, `1.234.567` in German, `1 234 567` in French.
 */
export function formatNumber(value: number): string {
  return numberFormatter().format(value);
}

/** The spans `formatRelative` measures in, coarsest last. */
const SECOND = 1;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

/**
 * How long ago, in the coarsest unit that still says something: "3 days ago",
 * "in 2 months". Below a minute it counts seconds, because "now" twice in a
 * row is not an answer.
 */
export function formatRelative(value: Primitive, now: Date = new Date()): string {
  const seconds = (toNumber(value) - now.getTime()) / 1000;
  const magnitude = Math.abs(seconds);
  // The boundary is the *smaller* unit's span: "59 minutes" is what a reader
  // wants at 59 minutes, and rounding it up to "1 hour" would age the figure
  // by a minute every time the screen re-renders.
  if (magnitude < MINUTE) return relativeFormatter().format(Math.round(seconds), "second");
  if (magnitude < HOUR) return relativeFormatter().format(Math.round(seconds / MINUTE), "minute");
  if (magnitude < DAY) return relativeFormatter().format(Math.round(seconds / HOUR), "hour");
  if (magnitude < WEEK) return relativeFormatter().format(Math.round(seconds / DAY), "day");
  if (magnitude < MONTH) return relativeFormatter().format(Math.round(seconds / WEEK), "week");
  if (magnitude < YEAR) return relativeFormatter().format(Math.round(seconds / MONTH), "month");
  return relativeFormatter().format(Math.round(seconds / YEAR), "year");
}

/**
 * How long something lasts: `3 days`, `1 hour`.
 *
 * A duration is not a relative time. `Intl.RelativeTimeFormat` would answer
 * "in 3 days" for a redaction window, which is a claim about the future that
 * nobody made; `style: "unit"` pluralizes the unit instead, so the "1 hour"
 * against the "6 hours" is the formatter's business and not a branch in a
 * screen.
 *
 * `unitDisplay: "long"` because the short form is an abbreviation ("1 hr")
 * that the console has never shown a reader.
 */
export function formatDuration(value: number, unit: Intl.RelativeTimeFormatUnit): string {
  return numberFormatter({ style: "unit", unit, unitDisplay: "long" }).format(value);
}
