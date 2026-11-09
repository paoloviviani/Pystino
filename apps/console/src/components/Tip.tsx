import { Popover } from "@base-ui/react/popover";

/**
 * The (?) next to a label: the explanation lives here, so the screen itself
 * stays terse (ADR 0113's console). A button that opens on click or tap, and
 * on hover too, so it works with a pointer, a keyboard and a phone alike (a
 * tooltip opens only on hover or focus, which a touch screen never gives it).
 * Escape and a click outside close it; the popup keeps inside the viewport.
 */
export function Tip({ text }: { text: string }) {
  return (
    <Popover.Root>
      <Popover.Trigger
        openOnHover
        delay={150}
        aria-label={text}
        className="inline-flex size-5 shrink-0 cursor-help items-center justify-center rounded-full border border-line-strong bg-transparent p-0 text-dense text-ink-muted"
      >
        ?
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner sideOffset={6} collisionPadding={8} className="z-50 outline-none">
          <Popover.Popup className="max-w-72 rounded-md bg-ink px-2.5 py-1.5 text-dense text-ink-inverse shadow-md transition-opacity duration-100 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0">
            {text}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
