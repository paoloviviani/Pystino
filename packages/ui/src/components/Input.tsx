import type { InputHTMLAttributes, ReactNode } from "react";
import { useId } from "react";
import { cx } from "../cx";
import { controlClass, errorClass, hintClass, labelClass } from "./controls";

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
    <div className="flex flex-col gap-1">
      <label className={cx(labelClass, hideLabel && "sr-only")} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        // Not `error && …`: a `ReactNode` may legitimately be `0`, and a
        // falsy-prop guard that passes numbers through is how a class list
        // ends up containing the string "0".
        className={controlClass(cx(error ? "border-danger" : undefined, className))}
        aria-invalid={error ? true : undefined}
        // Points assistive technology at whichever explanation is showing, so
        // the reason a field is rejected is announced with the field itself.
        aria-describedby={error ? errorId : hint ? hintId : undefined}
        {...rest}
      />
      {error ? (
        <p id={errorId} className={errorClass}>
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className={hintClass}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
