/**
 * The period options offered in the picker.
 *
 * Named periods only — `2026-08`, `2026-Q3`, `2026` — never "last 30 days". The
 * gateway resolves a named period in the billing timezone, so the same choice
 * returns the same figures whenever it is asked, which is what makes a number on
 * this screen reconcilable against an invoice. A rolling window cannot be.
 *
 * The labels are built from the *current* date only to decide which periods to
 * offer; the values themselves are absolute.
 */

export interface PeriodOption {
  value: string;
  label: string;
}

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/**
 * The last `months` calendar months, then the current quarter and year.
 *
 * `now` is injectable so the list is testable without waiting for a month to
 * turn over — the same reason `gateway.periods` takes an explicit clock.
 */
export function recentPeriods(now: Date = new Date(), months = 6): PeriodOption[] {
  const options: PeriodOption[] = [];

  for (let back = 0; back < months; back += 1) {
    // Built from the calendar fields rather than by subtracting milliseconds:
    // arithmetic on a timestamp gets the wrong month around a DST shift and at
    // the end of a long month, and both are exactly when a report is run.
    const year = now.getFullYear();
    const month = now.getMonth() - back;
    const date = new Date(year, month, 1);
    const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
    const name = `${MONTH_NAMES[date.getMonth()]} ${date.getFullYear()}`;
    options.push({ value, label: back === 0 ? `${name} (current)` : name });
  }

  const quarter = Math.floor(now.getMonth() / 3) + 1;
  options.push({
    value: `${now.getFullYear()}-Q${quarter}`,
    label: `Q${quarter} ${now.getFullYear()}`,
  });
  options.push({ value: `${now.getFullYear()}`, label: `${now.getFullYear()} (year)` });

  return options;
}
