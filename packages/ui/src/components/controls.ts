import { cx } from "../cx";

/**
 * The one control chrome, shared by every native form control the package
 * renders (`Input`, `Select`).
 *
 * Kept in exactly one place so a field cannot drift from a select: two
 * components claiming to be the same kind of control with two different
 * borders is how a form starts looking hand-made again.
 */
export function controlClass(className?: string): string {
  return cx(
    "w-full rounded-md border border-line bg-surface px-3 py-2",
    "text-body text-ink placeholder:text-ink-faint",
    // iOS Safari zooms the page when a focused field is under 16px, so on a
    // phone a control is 16px: the one size outside the scale, and deliberate.
    "max-[40rem]:text-[16px]",
    "transition-shadow hover:border-line-strong",
    "focus-visible:outline-none focus-visible:shadow-focus",
    "disabled:cursor-not-allowed disabled:bg-sunken disabled:text-ink-muted",
    className,
  );
}

/** The field label, and the two lines that can sit under a control. */
export const labelClass = "text-dense font-medium tracking-[0.01em] text-ink-muted";
export const hintClass = "text-small text-ink-faint";
export const errorClass = "text-dense text-danger";
