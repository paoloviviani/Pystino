import type { ReactNode } from "react";
import { cx } from "../cx";

export interface SummaryStripProps {
  /** What the strip summarises, in words: "4 providers", "12 rules". */
  headline: ReactNode;
  /** The state beside the count: "all active", "2 over budget". */
  detail?: ReactNode;
  /** Actions on the right — usually relocated from the card header, never
   * duplicated from it. Two "Export CSV" buttons, one working while the table
   * loads and one not, is worse than the strip having none. */
  actions?: ReactNode;
  /**
   * Whether there is something to summarise. True is the tinted strip (the
   * accent-subtle fill); false is the sunken fill with the tile greyed out —
   * the chat's `STRIP_ACTIVE` / `STRIP_IDLE` pair. Defaults to true.
   */
  active?: boolean;
  /** Override for the tile glyph. As with `EmptyState`, screens share the
   * default rows mark rather than each drawing their own. */
  icon?: ReactNode;
}

/*
 * The tinted summary strip under a header (`overlay/styles.ts`'s `STRIP`): a
 * `size-10 rounded-xl` accent-wash icon tile, a count, a state, actions on the
 * right. It answers the question a table's header row cannot — "how many, and
 * are they well" — before the reader starts scanning rows.
 *
 * The tile keeps the translucent accent wash (`bg-accent/15`) rather than the
 * subtle fill, for the reason the chat's constant gives: when the strip behind
 * it is the same subtle fill, a tile in that fill disappears into it. The
 * opacity modifier reads the token, so the wash follows the theme like every
 * other utility here. Idle strips add `grayscale` to the tile, which is the
 * chat's way of saying "nothing here" without dimming the words beside it —
 * the count stays full ink because zero rules is still a fact worth reading.
 *
 * Added as a primitive because seven screens want this exact block. A strip
 * with one caller would have stayed inline.
 */
export function SummaryStrip({ headline, detail, actions, active = true, icon }: SummaryStripProps) {
  return (
    <div
      className={cx(
        "flex justify-between gap-4 rounded-lg p-4",
        "max-sm:flex-col max-sm:gap-4 sm:items-center",
        active ? "bg-accent-subtle" : "bg-sunken",
      )}
    >
      <div className="flex items-center gap-3">
        <div
          aria-hidden="true"
          className={cx(
            "flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent",
            !active && "grayscale",
          )}
        >
          {icon ?? <RowsIcon />}
        </div>
        <div>
          <div className="text-sm font-semibold text-ink">{headline}</div>
          {detail ? <div className="mt-0.5 text-xs text-ink-muted">{detail}</div> : null}
        </div>
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}

/** Three rows, for a strip that summarises rows. See `EmptyState`'s tray icon
 * for why this is drawn rather than imported. */
function RowsIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-5"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
    >
      <path d="M4 6.5h16M4 12h16M4 17.5h16" />
    </svg>
  );
}
