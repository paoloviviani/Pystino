import { cx } from "../cx";

export interface SpinnerProps {
  /** Announced to assistive technology; also the tooltip. */
  label?: string;
}

/** A quiet loading indicator for a panel that is fetching. */
export function Spinner({ label = "Loading" }: SpinnerProps) {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-6 text-sm text-ink-muted" role="status">
      <span
        aria-hidden="true"
        className={cx(
          "size-3.5 rounded-full border-2 border-line-strong border-t-accent",
          "animate-spin",
        )}
      />
      <span>{label}…</span>
    </div>
  );
}
