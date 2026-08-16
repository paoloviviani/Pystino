import { Button } from "./Button";
import styles from "./Pagination.module.css";

export interface PaginationProps {
  /** Rows the filter matched, not rows on this page. */
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  /** Plural noun for the count, e.g. "users". */
  noun?: string;
  /** Dims the range while a new page is being fetched. */
  busy?: boolean;
}

/**
 * Previous/next over an offset window, with the range spelled out.
 *
 * The range matters more than the buttons. An operator who has searched and is
 * looking at fifty rows needs to know whether that is all of them, and "1–50 of
 * 812" is the only thing on the page that says so. Without it a truncated
 * listing is indistinguishable from a complete one — which is the failure this
 * component exists to prevent, not a nicety.
 *
 * Numbered page links are deliberately absent. They cost a row of controls and
 * buy an operator nothing they cannot get by narrowing the search, which is
 * faster than paging anyway.
 */
export function Pagination({
  total,
  limit,
  offset,
  onOffsetChange,
  noun = "rows",
  busy = false,
}: PaginationProps) {
  // One page and nothing to say: rendering "1–3 of 3" next to two dead buttons
  // is noise on every small table in the console.
  if (total <= limit && offset === 0) return null;

  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  const hasPrevious = offset > 0;
  const hasNext = offset + limit < total;

  return (
    <nav className={styles.bar} aria-label="Pagination">
      <span className={styles.range} aria-live="polite" data-busy={busy || undefined}>
        {total === 0 ? (
          `No ${noun}`
        ) : (
          <>
            <strong>
              {first.toLocaleString()}–{last.toLocaleString()}
            </strong>{" "}
            of {total.toLocaleString()} {noun}
          </>
        )}
      </span>
      <div className={styles.buttons}>
        <Button
          disabled={!hasPrevious}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Previous
        </Button>
        <Button disabled={!hasNext} onClick={() => onOffsetChange(offset + limit)}>
          Next
        </Button>
      </div>
    </nav>
  );
}
