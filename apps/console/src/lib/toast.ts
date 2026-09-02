import { useToast } from "@llmp/ui";

/**
 * The toast manager, or `null` when no `ToastProvider` is mounted.
 *
 * The console mounts the provider in `main.tsx`, so every real screen gets
 * toasts. The unit tests mount routes bare, and Base UI's `useToastManager`
 * throws without a provider — so a route that wants to say "saved" needs this
 * guard to stay testable. Without the provider the hook throws before it
 * registers any state (`useContext` only reads), so the hook sequence is
 * identical on every render of a given mount; calling it unconditionally is
 * safe in either world.
 *
 * Call sites read `toast?.add(...)` and every fact a toast carries must also
 * live in the screen's persistent UI (a `Notice`, a badge) — the toast is
 * feedback, never the record.
 */
export function useOptionalToast(): ReturnType<typeof useToast> | null {
  try {
    return useToast();
  } catch {
    return null;
  }
}
