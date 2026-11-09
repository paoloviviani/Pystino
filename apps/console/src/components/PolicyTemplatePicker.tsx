import { Button, Dialog } from "@llmp/ui";
import { MUTED } from "../lib/layout";
import {
  POLICY_TEMPLATES,
  skippedNote,
  type PolicyTemplate,
  type Skipped,
} from "../lib/policyTemplates";
import { m } from "../paraglide/messages.js";

/**
 * The picker for "New policy from template".
 *
 * Each template is a row: its name, what it is for, and a one-line note naming
 * what the redaction service cannot do for it, so a missing item is never a
 * surprise. Choosing one hands it to the caller, which opens an unsaved policy.
 * `skippedFor` is the caller's own reckoning (an administrator's is against the
 * service, a user's against what the administrator allowed).
 */
export function PolicyTemplatePicker({
  open,
  onClose,
  onPick,
  skippedFor,
  busy = false,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (template: PolicyTemplate) => void;
  skippedFor: (template: PolicyTemplate) => Skipped[];
  /** The service's capabilities have not arrived yet. */
  busy?: boolean;
}) {
  return (
    <Dialog
      open={open}
      title={m["policy_template.title"]()}
      onClose={onClose}
      footer={
        <Button variant="secondary" onClick={onClose}>
          {m["privacy.delete.cancel"]()}
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        <p className={`m-0 text-body ${MUTED}`}>{m["policy_template.lead"]()}</p>
        {POLICY_TEMPLATES.map((template) => {
          const note = skippedNote(skippedFor(template));
          return (
            <div
              key={template.id}
              className="flex flex-wrap items-start justify-between gap-3 border-b border-line-quiet pb-3"
            >
              <div className="min-w-0 flex-1 basis-56">
                <strong>{template.name()}</strong>
                <p className={`m-0 text-body ${MUTED}`}>{template.description()}</p>
                {note && (
                  <p className="m-0 text-small text-warn" data-testid={`skipped-${template.id}`}>
                    {note}
                  </p>
                )}
              </div>
              <Button
                variant="secondary"
                disabled={busy}
                aria-label={m["policy_template.use_aria"]({ name: template.name() })}
                onClick={() => onPick(template)}
              >
                {m["policy_template.use"]()}
              </Button>
            </div>
          );
        })}
      </div>
    </Dialog>
  );
}
