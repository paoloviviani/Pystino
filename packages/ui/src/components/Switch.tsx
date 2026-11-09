import { cx } from "../cx";

export interface SwitchProps {
  /** Accessible name; the visible label sits beside it, outside the button. */
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** `sm` for a row in a dense list, `md` for a form. */
  size?: "sm" | "md";
  className?: string;
}

/**
 * An on/off switch: a `role="switch"` button, because flipping it is a state
 * change rather than a form value, and `aria-checked` already says which.
 */
export function Switch({ label, checked, onChange, disabled, size = "md", className }: SwitchProps) {
  const track = size === "sm" ? "h-6 w-11" : "h-7 w-12";
  const knob = size === "sm" ? "size-4.5" : "size-5.5";
  const move = size === "sm" ? "translate-x-5" : "translate-x-5";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      data-checked={checked}
      onClick={() => onChange(!checked)}
      className={cx(
        "relative inline-flex shrink-0 cursor-pointer items-center rounded-full border border-line-strong bg-sunken p-0.5 transition-colors",
        "data-[checked=true]:border-accent data-[checked=true]:bg-accent",
        "disabled:cursor-not-allowed disabled:opacity-60",
        "focus-visible:outline-none focus-visible:shadow-focus",
        track,
        className,
      )}
    >
      <span
        aria-hidden
        className={cx(
          "rounded-full border border-line-strong bg-surface transition-transform",
          knob,
          checked ? move : "translate-x-0",
        )}
      />
    </button>
  );
}
