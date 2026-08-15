import styles from "./Money.module.css";

export interface MoneyProps {
  /** The API sends money as a decimal *string*, and it must stay one. */
  amount: string;
  currency: string;
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
export function Money({ amount, currency }: MoneyProps) {
  return (
    <span className={styles.money}>
      {formatMoney(amount, currency)}
    </span>
  );
}

/** Trims the API's trailing zeros to two decimals without going through a float. */
export function formatMoney(amount: string, currency: string): string {
  const symbol = SYMBOLS[currency.toUpperCase()] ?? `${currency.toUpperCase()} `;
  const negative = amount.startsWith("-");
  const [whole = "0", fraction = ""] = amount.replace("-", "").split(".");

  // Two decimals is the display convention for money. Sub-cent precision is real
  // in the ledger — a single cheap request can cost a fraction of a cent — so it
  // is shown rather than rounded away to a misleading 0.00.
  const trimmed = fraction.replace(/0+$/, "");
  const decimals = trimmed.length > 2 ? trimmed : fraction.slice(0, 2).padEnd(2, "0");

  // U+00A0, written as an escape so it is visible in review: a literal space
  // here is invisible in a diff, and a *breaking* one lets "1 234 567" wrap
  // across two lines in a narrow table cell.
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
  return `${negative ? "−" : ""}${symbol}${grouped}.${decimals}`;
}

const SYMBOLS: Record<string, string> = {
  EUR: "€",
  USD: "$",
  GBP: "£",
  CHF: "CHF ",
};
