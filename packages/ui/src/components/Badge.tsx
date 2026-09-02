import type { ReactNode } from "react";
import { cx } from "../cx";

export interface BadgeProps {
  tone?: "neutral" | "accent" | "ok" | "warn" | "danger";
  children: ReactNode;
}

/*
 * A tonal chip: a light container with dark type, not a saturated block —
 * six loud chips in a table row are the loudest thing on a screen (see the
 * CSS Modules this replaces). The one exception is `danger`, which stays
 * filled: it is the thing an operator is scanning for and should not have to
 * compete with five tonal siblings for attention.
 */
const TONES: Record<NonNullable<BadgeProps["tone"]>, string> = {
  neutral: "bg-sunken text-ink-muted",
  accent: "bg-accent-subtle text-accent",
  ok: "bg-ok-subtle text-ok",
  warn: "bg-warn-subtle text-warn",
  danger: "bg-danger text-ink-inverse",
};

export function Badge({ tone = "neutral", children }: BadgeProps) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5",
        "text-xs font-medium tracking-[0.01em] whitespace-nowrap",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}
