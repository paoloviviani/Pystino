import type { ReactNode } from "react";
import { cx } from "../cx";

export interface EmptyStateProps {
  /** What there is none of. Kept as the full sentence the screen always said
   * ("No quota rules. Nothing is capped.") rather than shortened: several
   * route tests assert that wording, and shortening it here would mean
   * rewording the behaviour the tests pin. */
  title: ReactNode;
  /** One line saying what it means or what to do next. */
  detail?: ReactNode;
  /** The primary call to action — usually the same button the header carries
   * for the non-empty case ("New rule"). Rendered only when there is something
   * to do; an empty state with no action is a fact, not a task. */
  action?: ReactNode;
  /** Override for the tray glyph. Screens share the default rather than each
   * drawing their own: seven bespoke outline icons are seven chances to drift,
   * and the icon here is signposting, not illustration. */
  icon?: ReactNode;
}

/*
 * The chat's dashed empty state (`overlay/styles.ts`'s `EMPTY`), as a
 * primitive: a dashed container, a large muted icon, a title, a detail line
 * and a primary call to action.
 *
 * Added as a primitive rather than left inline because more than half the
 * route screens need exactly this shape — every `Table` takes one as its
 * `empty`, and bare strings passed there are wrapped in it automatically, so
 * a listing that has nothing to show says so the same way everywhere. A shape
 * with one caller would have stayed inline; this one has a dozen.
 *
 * The border is the `strong` step, not the quiet one: a dashed rule in the
 * quiet grey dissolved into the card behind it, and a container nobody can
 * see is not a container.
 */
export function EmptyState({ title, detail, action, icon }: EmptyStateProps) {
  return (
    <div
      className={cx(
        "flex flex-col items-center justify-center rounded-lg",
        "border-2 border-dashed border-line-strong p-6 text-center",
      )}
    >
      <div aria-hidden="true" className="mb-3 size-10 text-ink-faint">
        {icon ?? <TrayIcon />}
      </div>
      <p className="m-0 text-sm font-medium text-ink">{title}</p>
      {detail ? <div className="mt-1 text-xs text-ink-muted">{detail}</div> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

/**
 * An empty tray, drawn rather than imported: the package has no icon
 * dependency and gaining one for a single glyph would be the tail wagging the
 * dog. Stroke, not fill — it sits in faint ink and a filled glyph at that
 * tone reads as a smudge.
 */
function TrayIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-full"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 9.5h5l1.5 2h11v8H3z" />
      <path d="M3 9.5V6.5h6l1.5 2" />
    </svg>
  );
}
