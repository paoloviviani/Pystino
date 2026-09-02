import type { ReactNode, SelectHTMLAttributes } from "react";
import { useId } from "react";
import { cx } from "../cx";
import { controlClass, hintClass, labelClass } from "./controls";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: ReactNode;
  /** Hides the label visually but keeps it for assistive technology. */
  hideLabel?: boolean;
  /** Guidance under the control, wired to it via `aria-describedby`. */
  hint?: ReactNode;
  children: ReactNode;
}

/**
 * A labelled select.
 *
 * Still the native `<select>` (ADR 0047): for a plain list of options it is the
 * best control on every platform — the mobile picker, the screen-reader
 * behaviour, and the keyboard model are all the browser's, not ours to
 * re-implement. The chevron is drawn by an SVG beside the control rather than a
 * background image, because a `data:` URL in a stylesheet has to be allowed by
 * the console's CSP `img-src`, and the answer to "can I inline this?" is meant
 * to stay no.
 */
export function Select({
  label,
  hideLabel = false,
  hint,
  className,
  children,
  ...rest
}: SelectProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  return (
    <div className="flex flex-col gap-1">
      <label className={cx(labelClass, hideLabel && "sr-only")} htmlFor={id}>
        {label}
      </label>
      <div className="relative">
        <select
          id={id}
          className={controlClass(cx("cursor-pointer appearance-none pr-9", className))}
          aria-describedby={hint ? hintId : undefined}
          {...rest}
        >
          {children}
        </select>
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="pointer-events-none absolute right-3 top-1/2 size-3.5 -translate-y-1/2 text-ink-faint"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m4 6 4 4 4-4" />
        </svg>
      </div>
      {hint ? (
        <p id={hintId} className={hintClass}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
