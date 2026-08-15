import type { ReactNode } from "react";
import styles from "./Notice.module.css";

export interface NoticeProps {
  tone?: "info" | "warn" | "danger";
  title?: ReactNode;
  children: ReactNode;
}

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
      className={[styles.notice, styles[tone]].join(" ")}
      // Errors should interrupt; a caveat should not.
      role={tone === "danger" ? "alert" : "note"}
    >
      {title && <p className={styles.title}>{title}</p>}
      <div className={styles.body}>{children}</div>
    </div>
  );
}
