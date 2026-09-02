import { Toast as BaseToast } from "@base-ui/react/toast";
import type { ReactNode } from "react";
import { cx } from "../cx";

/**
 * The console's toast plumbing, pre-assembled.
 *
 * Base UI's toast has six parts and a hook; every app that uses it assembles
 * the same four of them in the same corner of the screen. Assembling them once
 * here means a screen that wants to say "saved" writes one `add()` call and
 * never learns the part names (ADR 0047: structure without a framework).
 *
 * Tones ride on the manager's `type` string — `success`, `error`, `warning`,
 * anything else reads as neutral — and map to the same semantic palette the
 * inline `Notice` uses, so a caveat says the same thing in either shape.
 */
const TYPE_TONES: Record<string, string> = {
  success: "border-l-ok",
  error: "border-l-danger",
  warning: "border-l-warn",
};

export function useToast(): ReturnType<typeof BaseToast.useToastManager> {
  return BaseToast.useToastManager();
}

export function ToastProvider({ children }: { children: ReactNode }) {
  return (
    <BaseToast.Provider timeout={5000}>
      {children}
      <BaseToast.Portal>
        <BaseToast.Viewport className="fixed right-4 bottom-4 z-[60] flex w-96 max-w-[calc(100vw-2rem)] flex-col gap-2 outline-none">
          <ToastList />
        </BaseToast.Viewport>
      </BaseToast.Portal>
    </BaseToast.Provider>
  );
}

function ToastList() {
  const { toasts } = BaseToast.useToastManager();
  return toasts.map((toast) => (
    <BaseToast.Root
      key={toast.id}
      toast={toast}
      className={cx(
        "rounded-md border border-line-quiet border-l-4 bg-surface p-4 shadow-md",
        "transition-all duration-200",
        "data-[ending-style]:translate-y-2 data-[ending-style]:opacity-0",
        "data-[starting-style]:opacity-0",
        TYPE_TONES[toast.type ?? ""] ?? "border-l-ink",
      )}
    >
      <BaseToast.Content className="flex items-start justify-between gap-3">
        <div>
          <BaseToast.Title className="text-sm font-semibold text-ink" />
          {toast.description && (
            <BaseToast.Description className="mt-0.5 text-sm text-ink-muted" />
          )}
        </div>
        <BaseToast.Close className="cursor-pointer text-sm text-ink-faint transition-colors hover:text-ink">
          Dismiss
        </BaseToast.Close>
      </BaseToast.Content>
    </BaseToast.Root>
  ));
}
