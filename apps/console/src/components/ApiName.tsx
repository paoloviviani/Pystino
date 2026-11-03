import { Button } from "@llmp/ui";
import { useState } from "react";

import { CODE } from "../lib/layout";
import { m } from "../paraglide/messages.js";

/**
 * A private model's name as `/v1` knows it, `my/<name>`, with a copy button.
 *
 * The prefix belongs to the API and is not stored: the console's own name for
 * the row has none, and what a client must send does. Showing both, with the
 * second one copyable, is what saves a user from sending the bare name and
 * getting a 404 for a model they can see.
 */
export function ApiName({ name }: { name: string }) {
  const full = `my/${name}`;
  const [copied, setCopied] = useState<boolean | null>(null);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(full);
      setCopied(true);
    } catch {
      // No clipboard on an insecure origin, or permission denied. The name is
      // selectable text either way; do not let the button look as if it worked.
      setCopied(false);
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <code className={CODE}>{full}</code>
      <Button
        variant="ghost"
        aria-label={m["byok.copy_name_aria"]({ name: full })}
        onClick={copy}
      >
        {copied === true ? m["byok.copied"]() : m["byok.copy"]()}
      </Button>
      {copied === false && <span className="text-body text-warn">{m["byok.copy_failed"]()}</span>}
    </span>
  );
}
