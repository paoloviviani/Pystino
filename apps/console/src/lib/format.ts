import { activeLocale } from "./locale";

/**
 * Every date, number and amount the console shows, in one place.
 *
 * Screens used to call `toLocaleString()` with `undefined` as the locale, which
 * is the *browser's* language — so a reader whose browser is set to German saw
 * German formatting inside an English console, and the same console rendered
 * two different date columns on two machines. These helpers name the locale
 * explicitly, and it is the one the console is actually in: the catalogue's
 * locale, so the language of the words and the shape of the figures are
 * decided together. A future `<de>` catalogue therefore brings German
 * formatting with it, without a screen changing.
 *
 * None of them take a locale argument. A formatter that can be handed the
 * wrong locale is a formatter that will be, the first time somebody needs
 * "the reader's locale" for one field and "the console's" for the next.
 */

type Primitive = string | number | Date;

/**
 * `Intl` constructors are not free and a screen re-renders a lot, so each one
 * is built once per (locale, style). The locale is part of the key: it is
 * fixed for the life of a page, and a cache that outlived a change of it would
 * be a screen in one language showing another language's figures.
 */
const dates = new Map<string, Intl.DateTimeFormat>();
const numbers = new Map<string, Intl.NumberFormat>();
const relatives = new Map<string, Intl.RelativeTimeFormat>();

function dateFormatter(style: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify([activeLocale(), style]);
  const built = dates.get(key) ?? new Intl.DateTimeFormat(activeLocale(), style);
  dates.set(key, built);
  return built;
}

function numberFormatter(style?: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = JSON.stringify([activeLocale(), style]);
  const built = numbers.get(key) ?? new Intl.NumberFormat(activeLocale(), style);
  numbers.set(key, built);
  return built;
}

function relativeFormatter(): Intl.RelativeTimeFormat {
  const key = activeLocale();
  const built = relatives.get(key) ?? new Intl.RelativeTimeFormat(key, { numeric: "auto" });
  relatives.set(key, built);
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
 * `Oct 16, 2026` in English, `16 Oct 2026` in Italian, whatever the console's
 * language is.
 *
 * The style is not a preference, it is what every existing screen prints, and
 * a column of mixed date formats is harder to read than a uniform one.
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
 * A count, grouped the way the active locale groups: `1,234,567` in English,
 * `1.234.567` in German, `1 234 567` in French.
 */
export function formatNumber(value: number): string {
  return numberFormatter().format(value);
}

/**
 * An amount, in the console's currency.
 *
 * **Not for ledger money.** The API sends an amount as a decimal *string* and
 * it must stay one: `formatMoney` in `@llmp/ui` formats the digits with string
 * arithmetic for exactly that reason, rounds to milli-units, and never rounds a
 * real amount to zero. This helper takes a `number`, so anything that came off
 * the wire must not come here — it belongs to figures the console has already
 * turned into numbers (a percentage, a per-request average), and to screens
 * that are showing a currency for orientation rather than for an invoice.
 */
export function formatCurrency(value: number, currency = "EUR"): string {
  return numberFormatter({ style: "currency", currency }).format(value);
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
