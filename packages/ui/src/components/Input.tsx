import type { InputHTMLAttributes, ReactNode } from "react";
import { useId } from "react";
import styles from "./Input.module.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label: ReactNode;
  hideLabel?: boolean;
  /** Shown under the field. Use for units, formats, and consequences. */
  hint?: ReactNode;
  error?: ReactNode;
}

/**
 * A labelled text input.
 *
 * Label and control are wired together here rather than by the caller, for the
 * same reason as Select: an unlabelled input is the commonest accessibility
 * defect in an admin form, and making it impossible costs less than catching it
 * in review every time.
 */
export function Input({ label, hideLabel = false, hint, error, className, ...rest }: InputProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;

  return (
    <div className={styles.field}>
      <label className={hideLabel ? styles.labelHidden : styles.label} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={[styles.input, error ? styles.invalid : "", className].filter(Boolean).join(" ")}
        aria-invalid={error ? true : undefined}
        // Points assistive technology at whichever explanation is showing, so
        // the reason a field is rejected is announced with the field itself.
        aria-describedby={error ? errorId : hint ? hintId : undefined}
        {...rest}
      />
      {error ? (
        <p id={errorId} className={styles.error}>
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
