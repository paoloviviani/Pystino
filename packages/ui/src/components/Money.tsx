import styles from "./Money.module.css";

export interface MoneyProps {
  /** The API sends money as a decimal *string*, and it must stay one. */
  amount: string;
  currency: string;
  /** See `FormatMoneyOptions.maxDecimals`. Unset shows full stored precision. */
  maxDecimals?: number;
}

/**
 * Renders an amount the API produced.
 *
 * The amount arrives as a string because the backend stores money as
 * `Numeric(24,12)` and never as a float. Parsing it here with `Number()` would
 * reintroduce binary floating point at the last possible moment — so the digits
 * are formatted, never arithmetic'd. Any total shown must have been summed by
 * the API, not in the browser.
 */
export function Money({ amount, currency, maxDecimals }: MoneyProps) {
  return (
    <span className={styles.money}>
      {formatMoney(amount, currency, { maxDecimals })}
    </span>
  );
}

/**
 * Expands scientific notation into plain digits, exactly.
 *
 * `Numeric(24,12)` round-trips a zero as `Decimal("0E-12")`, and Python
 * serialises that verbatim. Fed straight into the formatter below it lost its
 * sign to `replace("-", "")` and came out as `0E12.00`. The gateway now sends
 * plain digits, so this is a second line of defence rather than the fix — but
 * a money formatter that silently mangles a valid decimal string is a trap
 * worth closing, and doing it with string shifts keeps the no-float promise
 * that is the whole point of this module.
 */
function expandExponent(amount: string): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?[eE]([+-]?\d+)$/.exec(amount);
  if (!match) return amount;

  const [, sign = "", whole = "", fraction = "", exponent = "0"] = match;
  const digits = `${whole}${fraction}`;
  // Where the point sits once the exponent is applied, counted from the left.
  // `Number` on the *exponent* is safe — it is a small integer, not the amount.
  const point = whole.length + Number(exponent);

  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

/**
 * Half-up rounding of a decimal string, by string arithmetic.
 *
 * `Number()` is not used, for the reason this whole module exists: the amount
 * may carry twelve decimal places and turning it into a float to round it would
 * reintroduce binary floating point at the last moment. Carrying by hand is a
 * dozen lines and exact.
 *
 * Rounds rather than truncates, because truncation always understates a bill,
 * and a figure that is quietly low is worse than one that is visibly rounded.
 */
function roundTo(whole: string, fraction: string, places: number): [string, string] {
  if (fraction.length <= places) return [whole, fraction.padEnd(places, "0")];

  const kept = fraction.slice(0, places);
  const roundUp = Number(fraction[places]) >= 5; // one digit, so `Number` is safe
  if (!roundUp) return [whole, kept];

  // Propagate the carry through the kept fraction and, if it survives, the
  // integer part. `999.99` at two places must become `1000.00`, not `1000.99`.
  const digits = `${whole}${kept}`.split("");
  let index = digits.length - 1;
  for (; index >= 0; index -= 1) {
    if (digits[index] === "9") {
      digits[index] = "0";
    } else {
      digits[index] = String(Number(digits[index]) + 1);
      break;
    }
  }
  const carried = index < 0 ? ["1", ...digits] : digits;
  const cut = carried.length - places;
  return [carried.slice(0, cut).join("") || "0", carried.slice(cut).join("")];
}

export interface FormatMoneyOptions {
  /**
   * Cap the decimals shown, rounding to fit.
   *
   * Left unset the full stored precision is shown, which is what an
   * administrator reconciling against a provider's invoice needs. Set it for a
   * reader who wants to know what they spent, not to audit it — three places
   * (milli-EUR) is the useful figure there, and twelve is noise.
   */
  maxDecimals?: number;
}

/** Trims the API's trailing zeros to two decimals without going through a float. */
export function formatMoney(
  input: string,
  currency: string,
  options: FormatMoneyOptions = {},
): string {
  const amount = expandExponent(input);
  const symbol = SYMBOLS[currency.toUpperCase()] ?? `${currency.toUpperCase()} `;
  const negative = amount.startsWith("-");
  const [rawWhole = "0", rawFraction = ""] = amount.replace("-", "").split(".");

  const { maxDecimals } = options;
  if (maxDecimals !== undefined) {
    const [whole, fraction] = roundTo(rawWhole, rawFraction, maxDecimals);
    const grouped = group(whole);
    const rendered = maxDecimals > 0 ? `${grouped}.${fraction}` : grouped;

    // Rounding a real amount down to zero would say the reader spent nothing.
    // That is the mistake the full-precision branch below exists to avoid, so
    // it must not be reintroduced by capping the decimals: a non-zero amount
    // below the smallest unit shown says so instead.
    const vanished = /^0*$/.test(`${whole}${fraction}`) && /[1-9]/.test(`${rawWhole}${rawFraction}`);
    if (vanished) {
      const smallest = maxDecimals > 0 ? `0.${"0".repeat(maxDecimals - 1)}1` : "1";
      // The inequality flips for a credit: a negative amount nearer zero than
      // the smallest shown unit is *greater* than minus that unit. "−< €0.001"
      // reads as nonsense, which is worse than being slightly verbose.
      return negative ? `> −${symbol}${smallest}` : `< ${symbol}${smallest}`;
    }
    return `${negative ? "−" : ""}${symbol}${rendered}`;
  }

  // Two decimals is the display convention for money. Sub-cent precision is real
  // in the ledger — a single cheap request can cost a fraction of a cent — so it
  // is shown rather than rounded away to a misleading 0.00.
  const trimmed = rawFraction.replace(/0+$/, "");
  const decimals = trimmed.length > 2 ? trimmed : rawFraction.slice(0, 2).padEnd(2, "0");
  return `${negative ? "−" : ""}${symbol}${group(rawWhole)}.${decimals}`;
}

// U+00A0, written as an escape so it is visible in review: a literal space here
// is invisible in a diff, and a *breaking* one lets "1 234 567" wrap across two
// lines in a narrow table cell.
function group(whole: string): string {
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
}

const SYMBOLS: Record<string, string> = {
  EUR: "€",
  USD: "$",
  GBP: "£",
  CHF: "CHF ",
};
