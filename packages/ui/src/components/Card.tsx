import type { ReactNode } from "react";
import styles from "./Card.module.css";

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
    <section className={styles.card}>
      {(title || actions) && (
        <header className={hasBody ? styles.header : styles.headerOnly}>
          <div>
            {title && <h2 className={styles.title}>{title}</h2>}
            {description && <p className={styles.description}>{description}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </header>
      )}
      {hasBody && <div className={flush ? styles.bodyFlush : styles.body}>{children}</div>}
    </section>
  );
}
