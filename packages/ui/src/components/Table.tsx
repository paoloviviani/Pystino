import type { ReactNode } from "react";
import styles from "./Table.module.css";

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

export function Table<Row>({
  columns,
  rows,
  rowKey,
  empty = "Nothing to show.",
  footer,
  caption,
}: TableProps<Row>) {
  if (rows.length === 0 && !footer) {
    return <div className={styles.empty}>{empty}</div>;
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        {/* Announced, not shown — see the note on `.caption`. Omitting it
            would leave a screen reader to infer what thirty numbers are. */}
        {caption && <caption className={styles.caption}>{caption}</caption>}
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={column.numeric ? styles.numeric : undefined}
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
                <td key={column.key} className={column.numeric ? styles.numeric : undefined}>
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
                <td key={column.key} className={column.numeric ? styles.numeric : undefined}>
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
