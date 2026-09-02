import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import type { ReactNode } from "react";
import { cx } from "../cx";

export interface DialogProps {
  open: boolean;
  title: ReactNode;
  /** Called on Escape, on a backdrop click, and by the close button. */
  onClose: () => void;
  /** Buttons, right-aligned. The confirming action goes last. */
  footer?: ReactNode;
  children: ReactNode;
}

/**
 * A modal dialog.
 *
 * Base UI supplies what the native `<dialog>` gave this component's previous
 * version — the focus trap, Escape, inertness behind the popup, correct
 * semantics — plus the portal, which a native element cannot do while staying
 * styled from tokens (the dialog element's backdrop pseudo-element cannot read
 * a utility class). Same trade every other primitive here makes: behaviour from
 * the library, pixels from the tokens (ADR 0047).
 *
 * The popup is positioned by utility classes rather than a Viewport wrapper:
 * a plain centered dialog needs no scroll containment, and the simple version
 * is the one that survives review.
 */
export function Dialog({ open, title, onClose, footer, children }: DialogProps) {
  return (
    <BaseDialog.Root
      open={open}
      // Routed through onClose rather than held in Root's state: the caller
      // owns `open`, and Escape/backdrop/close-button all agree with it.
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <BaseDialog.Portal>
        <BaseDialog.Backdrop
          className={cx(
            "fixed inset-0 bg-black/40 transition-opacity duration-150",
            "data-[ending-style]:opacity-0 data-[starting-style]:opacity-0",
          )}
        />
        <BaseDialog.Popup
          className={cx(
            "fixed top-1/2 left-1/2 z-50 w-[min(32rem,calc(100vw-2rem))]",
            "max-h-[85dvh] -translate-x-1/2 -translate-y-1/2 overflow-y-auto",
            "rounded-lg bg-surface shadow-md outline-none transition-opacity duration-150",
            "data-[ending-style]:opacity-0 data-[starting-style]:opacity-0",
          )}
        >
          <header className="flex items-center justify-between gap-4 border-b border-line-quiet px-5 py-4">
            <BaseDialog.Title className="text-md font-semibold leading-tight">
              {title}
            </BaseDialog.Title>
            <BaseDialog.Close
              aria-label="Close"
              className={cx(
                "flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-full text-lg text-ink-faint",
                "transition-colors hover:bg-sunken hover:text-ink",
                "focus-visible:outline-none focus-visible:shadow-focus",
              )}
            >
              ×
            </BaseDialog.Close>
          </header>
          <div className="px-5 py-4">{children}</div>
          {footer && (
            <footer className="flex flex-wrap justify-end gap-2 border-t border-line-quiet px-5 py-4">
              {footer}
            </footer>
          )}
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}
