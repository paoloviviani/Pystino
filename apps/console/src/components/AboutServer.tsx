import type { ReactNode } from "react";

import { m } from "../paraglide/messages.js";

export interface AboutServerProps {
  /** The summary line. Defaults to "About this server". */
  title?: string;
  children: ReactNode;
}

/**
 * Developer detail, folded away: a build hash, a file name, a command to run, an endpoint.
 *
 * An administrator who runs the server needs these now and then; the one who just manages users and
 * models should not meet them on the page they open every day. A native `<details>`, so it is
 * keyboard-operable and announced without any code here, and closed until someone asks.
 */
export function AboutServer({ title, children }: AboutServerProps) {
  return (
    <details className="group text-dense text-ink-muted">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-sm py-1 select-none hover:text-ink [&::-webkit-details-marker]:hidden">
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="size-3 shrink-0 transition-transform group-open:rotate-90"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m6 4 4 4-4 4" />
        </svg>
        {title ?? m["about_server.title"]()}
      </summary>
      <div className="mt-1 flex flex-col gap-1.5 pl-4.5 [overflow-wrap:anywhere]">
        {children}
      </div>
    </details>
  );
}
