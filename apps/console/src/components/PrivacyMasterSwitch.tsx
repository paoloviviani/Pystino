import { Button, Notice } from "@llmp/ui";
import { useState } from "react";

import { useRedactionStatus, useSetRedactionEngine } from "../lib/admin";
import { MUTED } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";
import type { RedactionStatus } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * "Privacy: On / Off", at the top of the Pipelines tab.
 *
 * Off means no pipeline runs and nothing is hidden: the `noop` engine, under a
 * name that says what it does. It is the same append-only engine row as before
 * (and the audit that goes with it); only the engine list is gone from the screen.
 * Turning it on picks the first installed engine that redacts. The detectors'
 * switches are carried over unchanged in both directions, or a round trip would
 * quietly reset them to the deployment default.
 */
export function PrivacyMasterSwitch() {
  const status = useRedactionStatus();
  const setEngine = useSetRedactionEngine();
  const toast = useOptionalToast();
  const [confirming, setConfirming] = useState(false);
  const data = status.data;
  // Strictly boolean: a status that has not loaded shows no switch rather than a guess.
  if (!data || typeof data.enabled !== "boolean") return null;
  const on = data.enabled;
  // The engine that was running before it was turned off, not merely the first that redacts.
  const redacting =
    data.engines.find(
      (engine) => engine.name === data.restore_engine && engine.blocked_reason === null,
    ) ?? data.engines.find((engine) => engine.redacts && engine.blocked_reason === null);
  const off = data.engines.find((engine) => !engine.redacts);

  const carry = (current: RedactionStatus) => ({
    presidio_pattern_matching: current.presidio_pattern_matching ?? null,
    presidio_ner: current.presidio_ner ?? null,
    spacy_enabled: current.spacy_enabled,
    gliner_enabled: current.gliner_enabled,
  });

  const change = (name: string) =>
    setEngine.mutate(
      {
        engine: name,
        reason: on ? m["privacy.master.reason_off"]() : m["privacy.master.reason_on"](),
        ...carry(data),
      },
      {
        onSuccess: () => {
          setConfirming(false);
          toast?.add({
            title: on ? m["privacy.master.off_toast"]() : m["privacy.master.on_toast"](),
            type: "success",
          });
        },
        onError: (error) =>
          toast?.add({
            title: m["admin_redaction.engine.toast_failed"](),
            // i18n: server text, the API's own error message.
            description: error instanceof Error ? error.message : m["admin_redaction.unknown_error"](),
            type: "error",
          }),
      },
    );

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line p-4" data-testid="master-switch">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="font-semibold">
            {m["privacy.master.label"]()}:{" "}
            {on ? m["privacy.master.on"]() : m["privacy.master.off"]()}
          </div>
          <div className={`text-small ${MUTED}`}>
            {on ? m["privacy.master.hint_on"]() : m["privacy.master.hint_off"]()}
          </div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={m["privacy.master.label"]()}
          disabled={setEngine.isPending || (on ? !off : !redacting)}
          onClick={() => {
            if (on) setConfirming(true);
            else if (redacting) change(redacting.name);
          }}
          data-checked={on}
          className="relative h-9 w-16 shrink-0 cursor-pointer rounded-full border border-line-strong bg-sunken transition-colors data-[checked=true]:border-accent data-[checked=true]:bg-accent disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline-none focus-visible:shadow-focus"
        >
          <span
            aria-hidden
            className={`absolute top-1 left-1 size-7 rounded-full border border-line-strong bg-surface transition-transform ${on ? "translate-x-7" : "translate-x-0"}`}
          />
        </button>
      </div>
      {confirming && (
        <div role="alertdialog" aria-label={m["privacy.master.confirm_title"]()} className="flex flex-col gap-2">
          <Notice tone="danger" title={m["privacy.master.confirm_title"]()}>
            {m["privacy.master.confirm_body"]()}
          </Notice>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="danger"
              disabled={setEngine.isPending}
              onClick={() => off && change(off.name)}
            >
              {m["privacy.master.confirm_off"]()}
            </Button>
            <Button variant="secondary" onClick={() => setConfirming(false)}>
              {m["admin_redaction.confirm.cancel"]()}
            </Button>
          </div>
        </div>
      )}
      {setEngine.error ? (
        <Notice tone="danger">
          {/* i18n: server text, the API's own error message. */}
          {setEngine.error instanceof Error
            ? setEngine.error.message
            : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}
    </div>
  );
}
