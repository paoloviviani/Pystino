import { useId, useState } from "react";

import { Notice } from "@llmp/ui";

import { DISCLOSURES } from "../lib/layout";

export interface DisclosuresProps {
  /** The report's disclosure strings. Nothing renders when there are none. */
  notes: string[];
  /** The localStorage key this page's open-or-closed choice is remembered under. */
  storageKey: string;
}

/**
 * The usage caveats, collapsed to one line.
 *
 * A long report shows several paragraphs above the numbers on every visit, so
 * the section starts collapsed: one warn-toned line naming the count and
 * carrying the first note (the browser clips it to the line at any width),
 * opening onto the notes exactly as the reporting pages used to render them.
 * The toggle is a button rather than a `<details>` because the state is
 * controlled — it is remembered per page and announced through
 * `aria-expanded`/`aria-controls` — and a natively keyboard-operable button
 * needs no re-implementation of any of that.
 *
 * The notes stay mounted behind `hidden` when collapsed: search and
 * assistive technology must not lose a caveat to a collapsed section. That
 * div carries no display class on purpose — a Tailwind display utility would
 * override the `hidden` attribute, whose `display: none` comes from the user
 * agent stylesheet and loses to any author rule.
 */

function readOpen(storageKey: string): boolean {
  try {
    return window.localStorage.getItem(storageKey) === "true";
  } catch {
    // A browser with site data blocked throws on access rather than
    // returning null; a choice that does not stick is a small loss, so the
    // section simply starts collapsed.
    return false;
  }
}

function writeOpen(storageKey: string, open: boolean): void {
  try {
    window.localStorage.setItem(storageKey, String(open));
  } catch {
    // See `readOpen`: not sticking is not failing.
  }
}

export function Disclosures({ notes, storageKey }: DisclosuresProps) {
  const [open, setOpen] = useState<boolean>(() => readOpen(storageKey));
  const contentId = useId();

  if (notes.length === 0) return null;

  const [first] = notes;
  const count = notes.length === 1 ? "1 note about these figures" : `${notes.length} notes about these figures`;

  return (
    <div className={DISCLOSURES}>
      <Notice tone="warn">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() =>
            setOpen((was) => {
              const next = !was;
              writeOpen(storageKey, next);
              return next;
            })
          }
          className="flex w-full cursor-pointer items-center gap-2 text-left"
        >
          <span className="shrink-0 font-semibold">{count} ·</span>
          <span className="min-w-0 flex-1 truncate">{first}</span>
          {/* The Select's chevron (Field.tsx): the same 16-grid inline SVG,
              drawn beside the control rather than invented here. It turns to
              point up when the section is open. */}
          <svg
            aria-hidden="true"
            viewBox="0 0 16 16"
            className={`pointer-events-none size-3.5 shrink-0 text-ink-faint transition-transform${open ? " rotate-180" : ""}`}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="m4 6 4 4 4-4" />
          </svg>
        </button>
      </Notice>
      <div id={contentId} hidden={!open} className="[&>*+*]:mt-2">
        {/* The API's own wording, verbatim. Restating a caveat in the UI is
            how the two end up disagreeing about what the number means. */}
        {notes.map((note) => (
          <Notice key={note} tone="warn">
            {note}
          </Notice>
        ))}
      </div>
    </div>
  );
}
