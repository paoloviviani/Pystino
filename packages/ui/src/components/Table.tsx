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
  /**
   * The label a stacked row (see `TableProps.stack`) puts before this value, for a header that is
   * not plain text. A string header is used as it is; a column with neither has no label (an
   * action column).
   */
  label?: string;
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
 * cell text is the 13px `text-dense` step of the type scale (tokens.css), and
 * padding is tight enough to keep a screenful of rows. Buttons inside cells
 * would undo that with their own padding, so they take the `sm` button size
 * here, once, rather than at each of the screens that put them in a row.
 * Monospace needs nothing: code inherits its neighbour's size.
 */
const TABLE =
  "w-full border-collapse text-dense " +
  "[&_td_button]:px-2.5 [&_td_button]:py-1 [&_td_button]:text-dense";
const CELL_X = "px-3 first:pl-5 last:pr-5";

/**
 * Stacked rows, under `sm`: every row becomes a small card, one "Label  value" line per column.
 * A column hidden only because the row is narrow (`hideBelow`) comes back here, since a card has the
 * room a table row does not; the header row goes to screen readers only. Literal classes, as above.
 */
const STACK_TABLE = "max-sm:block";
const STACK_HEAD = "max-sm:sr-only";
const STACK_BODY = "max-sm:block";
const STACK_ROW =
  "max-sm:block max-sm:border-b max-sm:border-line-quiet max-sm:px-4 max-sm:py-3 max-sm:last:border-b-0";
const STACK_CELL =
  "max-sm:flex max-sm:items-baseline max-sm:justify-between max-sm:gap-3 max-sm:border-0! max-sm:px-0! max-sm:py-1! max-sm:text-right " +
  "max-sm:before:shrink-0 max-sm:before:text-left max-sm:before:font-medium max-sm:before:text-ink-muted max-sm:before:content-[attr(data-label)]";
/** Between `sm` and the column's own breakpoint: hidden only where a table row is narrow but not stacked. */
const STACK_HIDE_BELOW = {
  sm: "",
  md: "max-md:sm:hidden",
  lg: "max-lg:sm:hidden",
} as const;

export interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  /** Shown in place of the body when there are no rows. */
  empty?: ReactNode;
  /** Rendered as a distinct final row. A total belongs in the table, not beside it. */
  footer?: Row;
  caption?: string;
  /**
   * Under `sm`, draw each row as a card (label and value per line) instead of a table that scrolls
   * sideways with its action column off the edge. Opt in for listings whose rows carry an action
   * column, a bar or a date that a phone would clip.
   */
  stack?: boolean;
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
  stack = false,
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
  const hide = (column: Column<Row>) =>
    column.hideBelow &&
    (stack ? STACK_HIDE_BELOW[column.hideBelow] : HIDE_BELOW[column.hideBelow]);
  const labelOf = (column: Column<Row>) =>
    column.label ??
    (typeof column.header === "string" ? column.header : undefined);

  return (
    <div className="overflow-x-auto">
      <table className={cx(TABLE, stack && STACK_TABLE)}>
        {/* Announced, not shown — see the note on the caption. Omitting it
            would leave a screen reader to infer what thirty numbers are. */}
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className={cx(stack && STACK_HEAD)}>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cx(
                  "border-b border-line bg-sunken py-1.5 text-left text-dense font-semibold tracking-[0.01em] whitespace-nowrap text-ink",
                  CELL_X,
                  cellTone(column.numeric),
                  hide(column),
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className={cx(stack && STACK_BODY)}>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)} className={cx(stack && STACK_ROW)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  data-label={stack ? labelOf(column) : undefined}
                  className={cx(
                    CELL_X,
                    "py-2 align-baseline",
                    stack && STACK_CELL,
                    // The quiet rule under every row *except* the last: the
                    // card's own edge closes the table, and a line above it
                    // read as a border under a border.
                    index < rows.length - 1 && "border-b border-line-quiet",
                    cellTone(column.numeric),
                    cellWrap(column),
                    hide(column),
                  )}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer && (
          <tfoot className={cx(stack && STACK_BODY)}>
            <tr className={cx(stack && STACK_ROW)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  data-label={stack ? labelOf(column) : undefined}
                  className={cx(
                    stack && STACK_CELL,
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
                    hide(column),
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
