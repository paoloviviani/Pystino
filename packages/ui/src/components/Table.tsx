import type { ReactNode } from "react";
import { cx } from "../cx";
import { useUiMessages } from "../messages";
import { EmptyState } from "./EmptyState";

export interface Column<Row> {
  key: string;
  header: ReactNode;
  /** Right-aligned and rendered with tabular figures. Use for money and counts. */
  numeric?: boolean;
  /**
   * Drop this column on screens narrower than the named breakpoint.
   *
   * For secondary columns only, and the test is whether the row still answers
   * the question it exists for without it. A table that keeps every column on a
   * phone does not stay readable — it scrolls sideways, and the columns that go
   * off the edge are the ones at the end, chosen by declaration order rather
   * than by importance. Hiding says which ones are secondary.
   */
  hideBelow?: "sm" | "md" | "lg";
  /**
   * Keep each cell on one line. For short values that read as one token — a
   * date, an address, a name — which otherwise break at a hyphen or a space
   * ("provider-" / "1") the moment the column is squeezed. Numeric columns are
   * always one line. A long value should be clamped by the cell instead.
   */
  nowrap?: boolean;
  render: (row: Row) => ReactNode;
}

/** Tailwind cannot see a class name built by interpolation, so these are literal. */
const HIDE_BELOW = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
} as const;

/*
 * Density. These are working tables an administrator scans for many rows, so
 * cell text is the 13px `text-xs` step (the page's 17px body made a six-column
 * row wider than the screen and three lines tall), and padding is tight enough
 * to keep a screenful of rows. Two things inside cells would undo that if left
 * alone: monospace spans (`CODE` is the 15px step) and buttons (17px, tall
 * padding), so both are scaled to the cell here, once, rather than at each of
 * the screens that put them in a row.
 */
const TABLE =
  "w-full border-collapse text-xs " +
  "[&_td_.font-mono]:text-xs [&_td_button]:px-2.5 [&_td_button]:py-1 [&_td_button]:text-xs";
const CELL_X = "px-3 first:pl-5 last:pr-5";

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
  empty,
  footer,
  caption,
}: TableProps<Row>) {
  const t = useUiMessages();
  const emptyContent = empty ?? t.tableEmpty;
  if (rows.length === 0 && !footer) {
    // A bare sentence becomes the dashed empty state rather than a centred
    // line of muted text: every listing with nothing to show says so the same
    // way (ADR 0077, stages 2 and 3). The sentence itself is kept verbatim as
    // the title — screens word their empties deliberately ("No quota rules.
    // Nothing is capped."), and the route tests pin several of them, so the
    // primitive takes the words it is given rather than rewriting them. A
    // caller that wants a detail line or a call to action passes an
    // `EmptyState` (or anything else) explicitly instead of a string.
    return (
      <div className="p-5">
        {typeof emptyContent === "string" ? <EmptyState title={emptyContent} /> : emptyContent}
      </div>
    );
  }

  const cellTone = (numeric?: boolean) =>
    cx(numeric && "text-right tabular-nums");
  const cellWrap = (column: Column<Row>) =>
    (column.nowrap || column.numeric) && "whitespace-nowrap";

  return (
    <div className="overflow-x-auto">
      <table className={TABLE}>
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
                  "border-b border-line bg-sunken py-1.5 text-left text-xs font-semibold tracking-[0.01em] whitespace-nowrap text-ink",
                  CELL_X,
                  cellTone(column.numeric),
                  column.hideBelow && HIDE_BELOW[column.hideBelow],
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
                    CELL_X,
                    "py-2 align-baseline",
                    // The quiet rule under every row *except* the last: the
                    // card's own edge closes the table, and a line above it
                    // read as a border under a border.
                    index < rows.length - 1 && "border-b border-line-quiet",
                    cellTone(column.numeric),
                    cellWrap(column),
                    column.hideBelow && HIDE_BELOW[column.hideBelow],
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
                    CELL_X,
                    "border-t border-line-strong py-2 font-bold",
                    cellTone(column.numeric),
                    cellWrap(column),
                    // Not optional. Hiding a column in the head and body but
                    // not here leaves the total row one cell wider than the
                    // rest, so every figure in it lands under the wrong header.
                    column.hideBelow && HIDE_BELOW[column.hideBelow],
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
