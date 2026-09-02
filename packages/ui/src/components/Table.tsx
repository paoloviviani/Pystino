import type { ReactNode } from "react";
import { cx } from "../cx";

export interface Column<Row> {
  key: string;
  header: ReactNode;
  /** Right-aligned and rendered with tabular figures. Use for money and counts. */
  numeric?: boolean;
  render: (row: Row) => ReactNode;
}

export interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  /** Shown in place of the body when there are no rows. */
  empty?: ReactNode;
  /** Rendered as a distinct final row. A total belongs in the table, not beside it. */
  footer?: Row;
  caption?: string;
}

/*
 * Row lines are the quiet rule, deliberately — the header rule and the total
 * rule are the strong ones, because they are the lines that separate *kinds* of
 * thing: header from body, body from total. (The rationale carried over from
 * the CSS Modules this replaces.)
 *
 * Borders are applied per-cell in the JSX rather than by ancestor selectors in
 * a stylesheet, so the "no line under the last body row" and "strong line above
 * the total" decisions stay next to the code that decides them.
 */
export function Table<Row>({
  columns,
  rows,
  rowKey,
  empty = "Nothing to show.",
  footer,
  caption,
}: TableProps<Row>) {
  if (rows.length === 0 && !footer) {
    return <div className="px-5 py-6 text-center text-ink-muted">{empty}</div>;
  }

  const cellTone = (numeric?: boolean) =>
    cx(numeric && "text-right tabular-nums");

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-base">
        {/* Announced, not shown — see the note on the caption. Omitting it
            would leave a screen reader to infer what thirty numbers are. */}
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cx(
                  "border-b border-line bg-sunken px-5 py-2 text-left text-xs font-medium tracking-[0.01em] whitespace-nowrap text-ink",
                  cellTone(column.numeric),
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cx(
                    "px-5 py-3 align-baseline",
                    // The quiet rule under every row *except* the last: the
                    // card's own edge closes the table, and a line above it
                    // read as a border under a border.
                    index < rows.length - 1 && "border-b border-line-quiet",
                    cellTone(column.numeric),
                  )}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer && (
          <tfoot>
            <tr>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cx(
                    // The total row keeps the body's cell padding: it was born
                    // from a stylesheet that padded every `td`, and the Tailwind
                    // rewrite styled body cells and forgot it — the total sat
                    // flush against the card's edge, looking cut off. Found by
                    // looking at the real console, not by a test.
                    "border-t border-line-strong px-5 py-3 font-bold",
                    cellTone(column.numeric),
                  )}
                >
                  {column.render(footer)}
                </td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}
