import type { ButtonHTMLAttributes, ReactNode } from "react";
import styles from "./Button.module.css";

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
      className={[styles.button, styles[variant], className].filter(Boolean).join(" ")}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {children}
    </button>
  );
}
