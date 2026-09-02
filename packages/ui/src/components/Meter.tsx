import { cx } from "../cx";

export interface MeterProps {
  /** Consumed so far, in the same unit as `limit`. */
  value: number;
  limit: number;
  /** Announced to assistive technology, which cannot see the bar. */
  label: string;
  /** Shown beside the bar. Pre-formatted by the caller — this does no maths on money. */
  caption?: string;
}

/**
 * A budget bar.
 *
 * Deliberately takes numbers, not the money strings the API returns: the ratio
 * is a *visual* quantity and a rounding error in a bar's width harms nobody,
 * whereas converting an amount to a float to display it would. The caller
 * formats the exact figure into `caption` and passes the approximation here.
 *
 * The track keeps its **fixed width** - 10rem, widened from the 7rem the CSS
 * Modules used at the operator's asking, and still one width everywhere: it
 * was flexible once, captions differ in width, and two bars both at 40% that
 * are not the same length cannot be compared - which is the only thing a
 * column of these is for.
 */
export function Meter({ value, limit, label, caption }: MeterProps) {
  const ratio = limit > 0 ? Math.min(value / limit, 1) : value > 0 ? 1 : 0;
  const percent = Math.round(ratio * 100);
  const tone = ratio >= 1 ? "over" : ratio >= 0.8 ? "near" : "under";

  return (
    <div className="flex flex-col items-start gap-1">
      <div
        className="h-2.5 w-40 overflow-hidden rounded-sm border border-line bg-surface"
        role="meter"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
        aria-valuetext={caption}
      >
        <div
          className={cx(
            "h-full transition-[width] duration-200",
            tone === "under" && "bg-ok",
            tone === "near" && "bg-yellow",
            tone === "over" && "bg-danger",
          )}
          style={{ width: `${percent}%` }}
        />
      </div>
      {caption && (
        // Under the bar, not beside it: stacked, the cell's width demand is
        // `max(bar, caption)` and no font metric or locale can make the caption
        // compete with the bar for horizontal space.
        <span className="text-sm whitespace-nowrap text-ink-muted tabular-nums">{caption}</span>
      )}
    </div>
  );
}
