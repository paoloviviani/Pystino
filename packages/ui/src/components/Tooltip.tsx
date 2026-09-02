import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import type { ReactElement, ReactNode } from "react";
import { cx } from "../cx";

export interface TooltipProps {
  label: ReactNode;
  /** The single element that triggers the tooltip. It must accept a ref. */
  children: ReactElement;
}

/**
 * A hover/focus hint, on Base UI's positioning and a11y wiring.
 *
 * `title` attributes remain the right tool for money amounts (`Money` keeps
 * one): a tooltip does not print and does not exist on touch, which is exactly
 * why that component keeps both. This exists for the cases a `title` cannot
 * do — rich content, a longer explanation on an icon-only control.
 */
export function Tooltip({ label, children }: TooltipProps) {
  return (
    <BaseTooltip.Provider>
      <BaseTooltip.Root>
        <BaseTooltip.Trigger render={children} />
        <BaseTooltip.Portal>
          <BaseTooltip.Positioner sideOffset={6} className="z-50 outline-none">
            <BaseTooltip.Popup
              className={cx(
                "max-w-72 rounded-md bg-ink px-2.5 py-1.5 text-xs text-ink-inverse shadow-md",
                "transition-opacity duration-100",
                "data-[ending-style]:opacity-0 data-[starting-style]:opacity-0",
              )}
            >
              {label}
            </BaseTooltip.Popup>
          </BaseTooltip.Positioner>
        </BaseTooltip.Portal>
      </BaseTooltip.Root>
    </BaseTooltip.Provider>
  );
}
