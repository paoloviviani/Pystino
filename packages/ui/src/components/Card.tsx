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
  return (
    <section className={styles.card}>
      {(title || actions) && (
        <header className={styles.header}>
          <div>
            {title && <h2 className={styles.title}>{title}</h2>}
            {description && <p className={styles.description}>{description}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </header>
      )}
      <div className={flush ? styles.bodyFlush : styles.body}>{children}</div>
    </section>
  );
}
