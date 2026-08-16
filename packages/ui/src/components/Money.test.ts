import { describe, expect, it } from "vitest";
import { formatMoney } from "./Money";

/**
 * The gateway stores money as `Numeric(24,12)` and sends it as a *string*
 * precisely so no float ever touches an amount. This function is the last place
 * that could undo it, so what is tested here is mostly that it does not: no
 * `Number()`, no rounding that loses a real sub-cent cost, no locale surprise.
 */
describe("formatMoney", () => {
  it("shows two decimals for an ordinary amount", () => {
    expect(formatMoney("12.500000000000", "EUR")).toBe("€12.50");
  });

  it("keeps sub-cent precision rather than rounding it to nothing", () => {
    // A single cheap request genuinely costs a fraction of a cent. Showing
    // "€0.00" for real spend makes the ledger look broken.
    expect(formatMoney("0.000000100000", "EUR")).toBe("€0.0000001");
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
    expect(formatMoney("1.5E-7", "EUR")).toBe("€0.00000015");
    expect(formatMoney("1.5E+3", "EUR")).toBe("€1\u00a0500.00");
    expect(formatMoney("-2E-3", "EUR")).toBe("−€0.002");
  });

  it("leaves an ordinary decimal string alone", () => {
    // The expansion must not touch the common case.
    expect(formatMoney("0.000000000000", "EUR")).toBe("€0.00");
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
