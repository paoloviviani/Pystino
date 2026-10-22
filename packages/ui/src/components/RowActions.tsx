import { useState } from "react";
import type { ReactNode } from "react";
import { cx } from "../cx";
import { MenuContent, MenuItem, MenuRoot, MenuSeparator, MenuTrigger } from "./Menu";

export interface RowAction {
  key: string;
  label: ReactNode;
  onSelect: () => void;
  /** Drawn in the danger ink and set apart from the other items by a rule. */
  destructive?: boolean;
  disabled?: boolean;
}

export interface RowActionsProps {
  /** The trigger's accessible name, naming the row: "Actions for gpt-4o". */
  label: string;
  actions: RowAction[];
}

const TRIGGER =
  "inline-flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-lg border border-line text-ink-muted transition-colors hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:shadow-focus data-popup-open:bg-sunken data-popup-open:text-ink";

/**
 * A table row's secondary actions behind one "⋯" button.
 *
 * Four buttons per row is what made the admin tables scroll sideways, and the
 * columns that fell off the edge were the actions. A menu keeps the row's width
 * to one small target; Base UI supplies the keyboard behaviour (arrows, typeahead,
 * Escape) and returns focus to the trigger on close. A destructive item stays
 * a menu item that *opens* the screen's confirmation, never one that acts.
 */
export function RowActions({ label, actions }: RowActionsProps) {
  // Until the first press this is a plain button. A listing of fifty rows
  // would otherwise mount fifty menu roots (each with its own positioner and
  // focus machinery) to draw fifty identical dots, and the cost shows on every
  // keystroke of a search box that re-renders the page.
  const [armed, setArmed] = useState(false);
  const safe = actions.filter((action) => !action.destructive);
  const risky = actions.filter((action) => action.destructive);
  if (actions.length === 0) return null;
  const item = (action: RowAction) => (
    <MenuItem
      key={action.key}
      disabled={action.disabled}
      onClick={action.onSelect}
      className={cx(
        action.destructive && "text-danger!",
        action.disabled && "cursor-not-allowed opacity-50",
      )}
    >
      {action.label}
    </MenuItem>
  );
  const dots = (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-5" fill="currentColor">
      <circle cx="3" cy="8" r="1.7" />
      <circle cx="8" cy="8" r="1.7" />
      <circle cx="13" cy="8" r="1.7" />
    </svg>
  );
  if (!armed) {
    return (
      <button
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded="false"
        className={TRIGGER}
        onClick={() => setArmed(true)}
      >
        {dots}
      </button>
    );
  }
  return (
    <MenuRoot defaultOpen>
      <MenuTrigger aria-label={label} className={TRIGGER}>
        {dots}
      </MenuTrigger>
      <MenuContent align="end" className="min-w-44">
        {safe.map(item)}
        {safe.length > 0 && risky.length > 0 && <MenuSeparator />}
        {risky.map(item)}
      </MenuContent>
    </MenuRoot>
  );
}
