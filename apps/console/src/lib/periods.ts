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

import { m } from "../paraglide/messages.js";

export interface PeriodOption {
  value: string;
  label: string;
}

/**
 * Month names, in calendar order.
 *
 * Messages rather than `Intl.DateTimeFormat`: a month name is half of a
 * sentence ("August 2026 (current)"), so it has to move with the language the
 * rest of the sentence is in, and a formatter with the browser's locale cannot
 * answer that. The label the API sends back is its own string, untouched.
 */
const MONTH_LABELS = [
  m["period.month.january"],
  m["period.month.february"],
  m["period.month.march"],
  m["period.month.april"],
  m["period.month.may"],
  m["period.month.june"],
  m["period.month.july"],
  m["period.month.august"],
  m["period.month.september"],
  m["period.month.october"],
  m["period.month.november"],
  m["period.month.december"],
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
    const monthIndex = now.getMonth() - back;
    const date = new Date(now.getFullYear(), monthIndex, 1);
    const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
    const monthName = MONTH_LABELS[date.getMonth()]?.() ?? "";
    // A year as a plain string, not a number: `{year: number}` would group it
    // as "2,026" in any locale that groups four digits.
    const year = String(date.getFullYear());
    options.push({
      value,
      label:
        back === 0
          ? m["period.month_with_year_current"]({ month: monthName, year })
          : m["period.month_with_year"]({ month: monthName, year }),
    });
  }

  const quarter = String(Math.floor(now.getMonth() / 3) + 1);
  const year = String(now.getFullYear());
  options.push({
    value: `${year}-Q${quarter}`,
    label: m["period.quarter"]({ quarter, year }),
  });
  options.push({ value: year, label: m["period.year"]({ year }) });

  return options;
}
