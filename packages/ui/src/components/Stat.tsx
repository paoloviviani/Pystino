import type { ReactNode } from "react";
import styles from "./Stat.module.css";

export interface StatProps {
  label: ReactNode;
  value: ReactNode;
  /** Smaller line under the value: a period, a comparison, a count. */
  detail?: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger";
}

/** One headline figure. Money and counts render with tabular figures. */
export function Stat({ label, value, detail, tone = "neutral" }: StatProps) {
  return (
    <div className={styles.stat}>
      <div className={styles.label}>{label}</div>
      <div className={[styles.value, styles[tone]].join(" ")}>{value}</div>
      {detail && <div className={styles.detail}>{detail}</div>}
    </div>
  );
}
