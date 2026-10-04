import { Button } from "@llmp/ui";
import { useState } from "react";
import { SECRET, SECRET_DETAIL, SECRET_ROW } from "../lib/layout";

/**
 * Where "how it works" points: the published documentation, not this deployment.
 *
 * Whether a deployment serves the documentation at all is the operator's
 * choice (the full stack does, under /docs/pystino/; a Pystino-only deployment
 * does not), so a same-origin relative link would be a 404 on some installs
 * and the console cannot tell which. The published site (GitHub Pages, built
 * from docs/ by mkdocs) is the one address that is the same everywhere. The
 * anchor is the heading in docs/coding-agents.md and has to move with it.
 */
export const OPENCODE_DOCS_URL =
  "https://paoloviviani.github.io/Pystino/coding-agents/" +
  "#point-opencode-at-the-gateway-with-a-script";

/**
 * The one-liner that points opencode at this gateway.
 *
 * The origin is the console's own: the script is served by the gateway, so the
 * address a person is looking at is the address that works from their terminal,
 * with no setting to get wrong. It holds no secret, so showing it before a key
 * exists is fine; the script asks for the key itself.
 */
export function opencodeCommand(): string {
  return `curl -fsSL ${window.location.origin}/opencode/install.sh | bash`;
}

/**
 * "Use an API key with opencode", with the command to do it and a way to read
 * how it works. Shown beside the key list and again in the dialog that shows a
 * freshly minted key, which is the moment someone has a key and no idea what to
 * do with it.
 */
export function OpencodeSetup({ heading }: { heading?: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const command = opencodeCommand();

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      // No clipboard API on an insecure origin, or permission denied. The
      // command is selectable text either way; say so rather than let the
      // button look as if it worked.
      setCopied(false);
    }
  };

  return (
    <div className={heading ? "grid gap-3 border-t border-line pt-4" : "grid gap-3"}>
      {heading ? <h3 className="text-sm font-semibold">{heading}</h3> : null}
      <p className={SECRET_DETAIL}>Use an API key with the opencode coding agent.</p>
      <div className={SECRET_ROW}>
        <code className={SECRET} aria-label="Command to configure opencode">
          {command}
        </code>
        <Button onClick={copy}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      {copied === false && (
        <p className="text-sm text-warn">
          Could not reach the clipboard. Select the command and copy it by hand.
        </p>
      )}
      <p className={SECRET_DETAIL}>
        It asks for the key, shows what it will change and asks before writing.{" "}
        <a
          href={OPENCODE_DOCS_URL}
          target="_blank"
          rel="noreferrer noopener"
          className="text-ink underline underline-offset-2 hover:text-ink-muted"
        >
          How it works
        </a>
      </p>
    </div>
  );
}
