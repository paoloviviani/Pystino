import { describe, expect, it } from "vitest";
import { formatMoney } from "./Money";

/**
 * The gateway stores money as `Numeric(24,12)` and sends it as a *string*
 * precisely so no float ever touches an amount. This function is the last place
 * that could undo it, so what is tested here is mostly that it does not: no
 * `Number()`, no rounding that loses a real sub-cent cost, no locale surprise.
 *
 * **The default is milli-units, and that is a reversal.** It used to be full
 * stored precision, with the cap opt-in — which meant a call site leaked twelve
 * decimal places by *saying nothing*, and four of the five screens said nothing.
 * `€0.003847493000` in a table is not a figure anybody reads. Exactness is now
 * the deliberate act, spelled `{ exact: true }`.
 */
describe("formatMoney, at the default precision", () => {
  it("writes an ordinary amount the way money is written", () => {
    // Not €12.500. "At most" milli-units, so the third place appears only when
    // it carries something.
    expect(formatMoney("12.500000000000", "EUR")).toBe("€12.50");
  });

  it("uses the third decimal when it carries something", () => {
    expect(formatMoney("0.003847493000", "EUR")).toBe("€0.004");
  });

  it("says 'less than' rather than claiming nothing was spent", () => {
    // The trap. A single cheap request genuinely costs a fraction of a cent, and
    // "€0.00" for real spend makes the ledger look broken.
    expect(formatMoney("0.000000100000", "EUR")).toBe("< €0.001");
  });

  it("never produces scientific notation", () => {
    expect(formatMoney("0.000000000001", "EUR")).not.toMatch(/e/i);
  });

  it("groups thousands so a large figure is readable at a glance", () => {
    expect(formatMoney("1234567.890000000000", "EUR")).toBe("€1\u00a0234\u00a0567.89");
  });

  it("handles a whole number with no decimal point", () => {
    expect(formatMoney("42", "EUR")).toBe("€42.00");
  });

  it("handles zero", () => {
    expect(formatMoney("0", "EUR")).toBe("€0.00");
  });

  it("expands scientific notation rather than mangling it", () => {
    // `Numeric(24,12)` hands back `Decimal("0E-12")` for a zero price, which
    // Python serialises verbatim. This used to lose the sign to a `replace`
    // and render as "€0E12.00" on the pricing screen.
    expect(formatMoney("0E-12", "EUR")).toBe("€0.00");
  });

  it("expands an exponent without going near a float", () => {
    expect(formatMoney("1.5E+3", "EUR")).toBe("€1\u00a0500.00");
    expect(formatMoney("-2E-3", "EUR")).toBe("−€0.002");
  });

  it("uses a real minus sign for a negative amount", () => {
    // U+2212, not a hyphen: it aligns with the digits in a tabular column.
    expect(formatMoney("-5.00", "EUR")).toBe("−€5.00");
  });

  it("knows the currencies the platform bills in", () => {
    expect(formatMoney("1.00", "USD")).toBe("$1.00");
    expect(formatMoney("1.00", "GBP")).toBe("£1.00");
  });

  it("falls back to the code for a currency it has no symbol for", () => {
    expect(formatMoney("1.00", "SEK")).toBe("SEK 1.00");
  });

  it("does not lose precision the way a float would", () => {
    // 0.1 + 0.2 famously is not 0.3 in binary floating point. This function
    // never adds anything — the API does the arithmetic — and this pins that.
    expect(formatMoney("0.300000000000", "EUR")).toBe("€0.30");
  });
});

describe("formatMoney with exact precision", () => {
  /**
   * What an administrator reconciling against a provider's invoice needs: a
   * divergence can be smaller than a milli-unit and still be the thing they are
   * looking for. Reached by the console's "Exact figures" toggle.
   */

  it("shows every digit the ledger holds", () => {
    expect(formatMoney("0.001497172000", "EUR", { exact: true })).toBe("€0.001497172");
  });

  it("keeps sub-cent precision rather than rounding it away", () => {
    expect(formatMoney("0.000000100000", "EUR", { exact: true })).toBe("€0.0000001");
    expect(formatMoney("1.5E-7", "EUR", { exact: true })).toBe("€0.00000015");
  });

  it("still writes an ordinary amount with two decimals", () => {
    expect(formatMoney("12.500000000000", "EUR", { exact: true })).toBe("€12.50");
    expect(formatMoney("0.000000000000", "EUR", { exact: true })).toBe("€0.00");
  });

  it("overrides a decimal cap rather than being overridden by one", () => {
    // The caller asking for exactness means it. A screen that passes both is
    // asking for its usual rounding *unless* the reader has asked to see
    // everything, which is exactly how the toggle reaches <Money>.
    expect(formatMoney("0.001497172000", "EUR", { maxDecimals: 3, exact: true })).toBe(
      "€0.001497172",
    );
  });
});

describe("formatMoney with an explicit cap", () => {
  /**
   * Two audiences, two precisions. Someone reading what they spent wants
   * milli-units; an administrator reconciling against a provider's invoice wants
   * every digit the ledger holds. A screen can still name its own cap — the
   * default is only what it gets for saying nothing.
   */

  it("rounds to the requested places", () => {
    expect(formatMoney("0.001497172000", "EUR", { maxDecimals: 3 })).toBe("€0.001");
    expect(formatMoney("12.3456", "EUR", { maxDecimals: 3 })).toBe("€12.346");
  });

  it("rounds half up rather than truncating", () => {
    // Truncation always understates a bill. A figure that is quietly low is
    // worse than one that is visibly rounded.
    expect(formatMoney("0.0015", "EUR", { maxDecimals: 3 })).toBe("€0.002");
    expect(formatMoney("0.0014999", "EUR", { maxDecimals: 3 })).toBe("€0.001");
  });

  it("carries into the integer part", () => {
    // The case a naive implementation gets wrong: 999.999 must not become
    // 1000.999 or 999.000.
    expect(formatMoney("999.9995", "EUR", { maxDecimals: 3 })).toBe("€1\u00a0000.00");
    expect(formatMoney("9.9999", "EUR", { maxDecimals: 3 })).toBe("€10.00");
  });

  it("says 'less than' rather than claiming nothing was spent", () => {
    // The trap this exists to avoid. Capping the decimals must not reintroduce
    // the misleading zero that full precision was chosen to prevent.
    expect(formatMoney("0.000000400000", "EUR", { maxDecimals: 3 })).toBe("< €0.001");
    expect(formatMoney("0.0000001", "EUR", { maxDecimals: 2 })).toBe("< €0.01");
  });

  it("shows a true zero as zero", () => {
    expect(formatMoney("0", "EUR", { maxDecimals: 3 })).toBe("€0.00");
    expect(formatMoney("0.000000000000", "EUR", { maxDecimals: 3 })).toBe("€0.00");
  });

  it("trims a trailing zero back to the two decimals money is written in", () => {
    // It used to pad, for column alignment. Reversed: €1.500 is not how an
    // amount is written, and `tabular-nums` keeps the digits aligned anyway.
    expect(formatMoney("1.5", "EUR", { maxDecimals: 3 })).toBe("€1.50");
    expect(formatMoney("1.5", "EUR", { maxDecimals: 6 })).toBe("€1.50");
  });

  it("keeps whole units whole when asked for none", () => {
    expect(formatMoney("1234.56", "EUR", { maxDecimals: 0 })).toBe("€1\u00a0235");
  });

  it("keeps the minus sign on a rounded negative", () => {
    expect(formatMoney("-0.0015", "EUR", { maxDecimals: 3 })).toBe("−€0.002");
    // The inequality flips: a credit nearer zero than the unit shown is
    // greater than minus that unit, not "minus less than" it.
    expect(formatMoney("-0.0000001", "EUR", { maxDecimals: 3 })).toBe("> −€0.001");
  });

  it("rounds when no cap is asked for, because that is now the default", () => {
    expect(formatMoney("0.001497172000", "EUR")).toBe("€0.001");
  });
});
