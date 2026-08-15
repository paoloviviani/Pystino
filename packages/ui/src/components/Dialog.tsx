import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import styles from "./Dialog.module.css";

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
 * A modal dialog, built on the native `<dialog>` element.
 *
 * Native rather than a div with a high z-index, because the browser then
 * provides the things a hand-rolled modal usually gets wrong: the focus trap,
 * Escape to close, inertness of the page behind it, and correct semantics for a
 * screen reader. All of that is otherwise a few hundred lines and a bug.
 */
export function Dialog({ open, title, onClose, footer, children }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // showModal() is what makes it modal; setting the `open` attribute directly
    // renders it inline with no backdrop and no focus trap.
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      // Fired by Escape as well as by close(); routing both through onClose
      // keeps React's state and the DOM's from disagreeing about what is open.
      onClose={onClose}
      onClick={(event) => {
        // The backdrop is part of the dialog element, so a click landing on the
        // element itself rather than on its content came from outside the panel.
        if (event.target === ref.current) onClose();
      }}
    >
      <div className={styles.panel}>
        <header className={styles.header}>
          <h2 className={styles.title}>{title}</h2>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className={styles.body}>{children}</div>
        {footer && <footer className={styles.footer}>{footer}</footer>}
      </div>
    </dialog>
  );
}
