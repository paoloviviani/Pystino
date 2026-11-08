import { Button, Dialog } from "@llmp/ui";
import { useState } from "react";

import { MUTED } from "../lib/layout";
import { m } from "../paraglide/messages.js";

/**
 * One overlay for the whole model (ADR 0113, section 0), so the screens stay
 * short: policies and pipelines, first match inside and all combine, the ladder,
 * whole files leave whole, and no "local".
 */
export function HowItWorks({ own = false }: { own?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
        {m["privacy.how.button"]()}
      </Button>
      <Dialog
        open={open}
        title={m["privacy.how.title"]()}
        onClose={() => setOpen(false)}
        footer={<Button onClick={() => setOpen(false)}>{m["privacy.how.close"]()}</Button>}
      >
        <ol className="m-0 flex list-decimal flex-col gap-3 pl-5 text-body">
          <li>
            <strong>{m["privacy.how.objects_title"]()}</strong> {m["privacy.how.objects"]()}
          </li>
          <li>
            <strong>{m["privacy.how.combine_title"]()}</strong> {m["privacy.how.combine"]()}
          </li>
          <li>
            <strong>{m["privacy.how.ladder_title"]()}</strong> {m["privacy.how.ladder"]()}
            <div className={`mt-1 text-small ${MUTED}`}>{m["privacy.how.ladder_more"]()}</div>
          </li>
          <li>
            <strong>{m["privacy.how.whole_title"]()}</strong> {m["privacy.how.whole"]()}
          </li>
          <li>
            <strong>{m["privacy.how.nolocal_title"]()}</strong> {m["privacy.how.nolocal"]()}
          </li>
          {own && (
            <li>
              <strong>{m["privacy.how.yours_title"]()}</strong> {m["privacy.how.yours"]()}
            </li>
          )}
        </ol>
      </Dialog>
    </>
  );
}
