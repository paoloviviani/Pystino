import styles from "./Spinner.module.css";

export interface SpinnerProps {
  /** Announced to assistive technology; also the tooltip. */
  label?: string;
}

/** A quiet loading indicator for a panel that is fetching. */
export function Spinner({ label = "Loading" }: SpinnerProps) {
  return (
    <div className={styles.wrap} role="status">
      <span className={styles.spinner} aria-hidden="true" />
      <span className={styles.label}>{label}…</span>
    </div>
  );
}
