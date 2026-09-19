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
          // `rounded-full` here is geometry, not vocabulary: a spinner *is* a
          // ring, and squaring it would not make it a button any more than
          // rounding the dialog's close button made that a status pill (which
          // is why the close button did change and this does not). The track
          // and the moving edge both read tokens, so no literal colours.
          "size-3.5 rounded-full border-2 border-line-strong border-t-accent",
          "animate-spin",
        )}
      />
      <span>{label}…</span>
    </div>
  );
}
