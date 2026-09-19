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
 * `rounded-lg`, not the pill ADR 0047 chose: ADR 0077 keeps that shape for
 * status pills and moves buttons to the chat's rectangle-with-soft-corners,
 * which is what `overlay/styles.ts`'s `PRIMARY`/`SECONDARY` already draw in
 * the chat (Cerea's `src/lib/components/overlay/styles.ts`).
 *
 * `duration-*` takes a raw number in Tailwind v4, so this reads as the same
 * value the CSS Modules used (120ms).
 */
const VARIANTS: Record<ButtonVariant, string> = {
  // The one place blue is a fill rather than a tint (the chat's `PRIMARY`).
  // The previous outline-ring primary read as secondary once every button
  // shared one radius — a page can have one button that means "press this",
  // and a ring around plain surface was not it. The border matches the fill
  // so it stays invisible, the same trick `danger` already used to keep every
  // variant at one footprint. Text is the literal `text-white`, not
  // `text-ink-inverse`: `--colour-accent-solid` is pinned to the same blue in
  // both themes (tokens.css), so its label has to be pinned too — the inverse
  // token follows the theme and would go near-black on a fill that never does.
  primary: "border-accent-solid bg-accent-solid text-white hover:bg-accent-solid-hover",
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
        "inline-flex items-center justify-center gap-2 rounded-lg border px-3 py-2",
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
