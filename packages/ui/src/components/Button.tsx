import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../cx";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /**
   * Shows a busy state and disables the button.
   *
   * Separate from `disabled` on purpose: a screen reader should hear "busy",
   * not "unavailable", and the two mean different things to someone deciding
   * whether to wait or to give up.
   */
  busy?: boolean;
  children?: ReactNode;
}

/*
 * The pill is kept from the MD3 language on purpose (ADR 0047): on a page
 * otherwise made of rectangles, a pill is unmistakably something you press.
 *
 * `duration-*` and `brightness-*` take raw numbers in Tailwind v4, so these
 * read as the same values the CSS Modules used (120ms, 0.92).
 */
const VARIANTS: Record<ButtonVariant, string> = {
  // The primary action is an *outline*, not a fill: a thick accent ring around
  // a plain surface with ink-dark text, at the operator's asking. The ring is
  // an inset box-shadow rather than a border so the button keeps the exact
  // footprint of its 1px-bordered siblings — a real 2px border makes the
  // primary in a dialog footer stand 2px taller than the Cancel beside it.
  // While focused, the ring yields to the shared focus ring (one box-shadow
  // property, two claims), which is the right trade: focus must be loud.
  primary:
    "bg-surface text-ink shadow-[inset_0_0_0_2px_var(--colour-accent)] hover:shadow-[inset_0_0_0_2px_var(--colour-accent-hover)]",
  secondary: "bg-surface border-line-strong text-ink hover:bg-sunken",
  ghost: "bg-transparent border-transparent text-ink-muted hover:bg-sunken hover:text-ink",
  danger: "bg-danger border-danger text-ink-inverse hover:brightness-92",
};

export function Button({
  variant = "secondary",
  busy = false,
  disabled,
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      // `type` defaults to "submit" inside a form, which turns any stray button
      // into an accidental form submission. Explicit unless overridden.
      type="button"
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-full border px-3 py-2",
        "text-base font-medium leading-tight whitespace-nowrap",
        "cursor-pointer transition-colors duration-120",
        "focus-visible:outline-none focus-visible:shadow-focus",
        "disabled:cursor-not-allowed disabled:opacity-55",
        "aria-busy:cursor-progress",
        VARIANTS[variant],
        className,
      )}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {children}
    </button>
  );
}
