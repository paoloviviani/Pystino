import type { ReactNode } from "react";
import { cx } from "../cx";

export interface NoticeProps {
  tone?: "info" | "warn" | "danger";
  title?: ReactNode;
  children: ReactNode;
}

/*
 * A pale wash with the semantic colour carried by the leading-edge bar, not
 * by a saturated background — a paragraph on cadmium yellow is hard to read,
 * and a report whose disclosures all shout has no way left to say that one of
 * them matters more (the reason the CSS Modules this replaces gave).
 */
const TONES: Record<NonNullable<NoticeProps["tone"]>, string> = {
  info: "bg-sunken border-line-quiet border-l-ink",
  warn: "bg-warn-subtle text-warn border-line-quiet border-l-yellow",
  danger: "bg-danger-subtle text-danger border-line-quiet border-l-red",
};

/**
 * A caveat or an error, stated in place.
 *
 * The reporting API returns plain-language disclosures ("3 of 120 requests have
 * estimated token counts") and they are rendered through this, verbatim. Wording
 * a caveat twice — once in the API and once in the UI — is how the two end up
 * disagreeing about what the number means.
 */
export function Notice({ tone = "info", title, children }: NoticeProps) {
  return (
    <div
      className={cx(
        "rounded-md border border-l-4 px-4 py-3 text-sm",
        "[&>*+*]:mt-1",
        TONES[tone],
      )}
      // Errors should interrupt; a caveat should not.
      role={tone === "danger" ? "alert" : "note"}
    >
      {title && <p className="font-semibold">{title}</p>}
      <div>{children}</div>
    </div>
  );
}
