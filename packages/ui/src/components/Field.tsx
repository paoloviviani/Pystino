import type { ReactNode, SelectHTMLAttributes } from "react";
import { useId } from "react";
import styles from "./Field.module.css";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: ReactNode;
  /** Hides the label visually but keeps it for assistive technology. */
  hideLabel?: boolean;
  children: ReactNode;
}

/**
 * A labelled select.
 *
 * The label is generated with a matching `id`/`htmlFor` pair rather than left to
 * the caller: an unlabelled control is the single commonest accessibility defect
 * in an admin UI, and making it impossible here is cheaper than catching it in
 * review every time.
 */
export function Select({ label, hideLabel = false, className, children, ...rest }: SelectProps) {
  const id = useId();
  return (
    <div className={styles.field}>
      <label className={hideLabel ? styles.labelHidden : styles.label} htmlFor={id}>
        {label}
      </label>
      <select id={id} className={[styles.select, className].filter(Boolean).join(" ")} {...rest}>
        {children}
      </select>
    </div>
  );
}
