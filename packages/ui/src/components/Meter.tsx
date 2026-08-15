import styles from "./Meter.module.css";

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
 */
export function Meter({ value, limit, label, caption }: MeterProps) {
  const ratio = limit > 0 ? Math.min(value / limit, 1) : value > 0 ? 1 : 0;
  const percent = Math.round(ratio * 100);
  const tone = ratio >= 1 ? "over" : ratio >= 0.8 ? "near" : "under";

  return (
    <div className={styles.meter}>
      <div
        className={styles.track}
        role="meter"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
        aria-valuetext={caption}
      >
        <div className={[styles.fill, styles[tone]].join(" ")} style={{ width: `${percent}%` }} />
      </div>
      {caption && <span className={styles.caption}>{caption}</span>}
    </div>
  );
}
