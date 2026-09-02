import type { ReactNode } from "react";
import { cx } from "../cx";

export interface CardProps {
  title?: ReactNode;
  /** Right-aligned slot in the header: a period picker, a button, a count. */
  actions?: ReactNode;
  /** Small explanatory line under the title. */
  description?: ReactNode;
  /** Removes the body padding, for a card whose content is a full-bleed table. */
  flush?: boolean;
  children?: ReactNode;
}

export function Card({ title, actions, description, flush = false, children }: CardProps) {
  // A card with a heading and nothing under it drew its header rule anyway, and
  // then an empty padded body below the rule — a line across the lower half of
  // the card separating the content from nothing. Visible on the administration
  // landing page, where every card is a title and a sentence.
  //
  // So the rule belongs to the *pair*, not to the header: it exists to separate
  // two things, and with one thing there is nothing to separate.
  const hasBody = children !== undefined && children !== null && children !== false;

  return (
    <section
      className={cx(
        "overflow-hidden rounded-lg border border-line-quiet bg-surface shadow-sm",
      )}
    >
      {(title || actions) && (
        <header
          className={cx(
            "flex flex-wrap items-start justify-between gap-4 px-5 py-4",
            // The rule is the quiet one, like the card's own edge: it separates
            // two regions of one object rather than drawing a division across it.
            hasBody && "border-b border-line-quiet",
          )}
        >
          <div>
            {title && <h2 className="text-md font-semibold leading-tight">{title}</h2>}
            {description && <p className="mt-1 text-sm text-ink-muted">{description}</p>}
          </div>
          {actions && (
            // Not shrunk (which squashed the controls) but allowed onto its own
            // line — on a phone the Breakdown card's picker and Export button do
            // not fit beside the title, and a hard `shrink-0` used to cut the
            // last button off entirely.
            <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
          )}
        </header>
      )}
      {hasBody && <div className={flush ? undefined : "p-5"}>{children}</div>}
    </section>
  );
}
