import { Tooltip } from "@llmp/ui";

/**
 * The (?) next to a label: the explanation lives here, so the screen itself
 * stays terse (ADR 0113's console). A button, so it takes focus and a keyboard
 * can reach the words as a pointer can.
 */
export function Tip({ text }: { text: string }) {
  return (
    <Tooltip label={text}>
      <button
        type="button"
        aria-label={text}
        className="inline-flex size-5 shrink-0 cursor-help items-center justify-center rounded-full border border-line-strong bg-transparent p-0 text-dense text-ink-muted"
      >
        ?
      </button>
    </Tooltip>
  );
}
