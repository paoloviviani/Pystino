import type { ReactNode } from "react";
import { cx } from "../cx";

export interface BadgeProps {
  tone?: "neutral" | "accent" | "ok" | "warn" | "danger";
  /** Optional leading glyph. The chat's pills always pair the tone with an
   * icon; the console's tables usually cannot spare the width, so this stays
   * opt-in rather than required. */
  icon?: ReactNode;
  children: ReactNode;
}

/*
 * A rounded-full status pill in one of five tonal pairings — the chat's pill
 * vocabulary (`overlay/styles.ts`'s `PILL` + `PILL_TONES`), carried through
 * the shared tokens rather than the chat's literal colours. `rounded-full`
 * belongs to pills now: ADR 0047 gave it to buttons, ADR 0077 moves buttons
 * to `rounded-lg` and reserves the pill for status, which is why the dialog's
 * close button and nothing else had to give the shape up.
 *
 * Every tone is a subtle fill with text one step deeper, never a saturated
 * block — six loud chips in a table row are the loudest thing on a screen.
 * `danger` used to be the exception, filled red on the ground that an operator
 * scans for it. That put the exception exactly where the rule matters most: a
 * filled chip in a row of tonal ones outshouts everything beside it, and the
 * confirm dialogs already own filled red as the shape of "this cannot be
 * undone" (the row-level delete buttons stay thin red text for the same
 * reason). A danger that needs finding is found by position and wording, not
 * by saturation.
 *
 * Text sits at the 700 step, not the chat's 600: the chat's pairings as
 * literally written fail the 4.5:1 floor the console holds (green-600 on
 * green-100 is 3.0:1, red-600 on red-100 is 4.0:1), while the 700 steps clear
 * it on the same fills — measured, both themes: light 4.5–5.5:1 across the
 * five tones (`warn` is tightest at 4.51:1, `accent` 5.49:1, `danger` 5.30:1,
 * `ok` 4.57:1, neutral 8.79:1), dark 5.8–8.7:1 throughout. The dark halves
 * re-derive rather than invert: 400-weight text over near-black subtle fills.
 */
const TONES: Record<NonNullable<BadgeProps["tone"]>, string> = {
  neutral: "bg-sunken text-ink-muted",
  accent: "bg-accent-subtle text-accent",
  ok: "bg-ok-subtle text-ok",
  warn: "bg-warn-subtle text-warn",
  danger: "bg-danger-subtle text-danger",
};

export function Badge({ tone = "neutral", icon, children }: BadgeProps) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full py-0.5 pr-2 pl-1.5",
        "text-xs font-medium whitespace-nowrap",
        TONES[tone],
      )}
    >
      {icon}
      {children}
    </span>
  );
}
