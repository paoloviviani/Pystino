import { Notice } from "@llmp/ui";
import { m } from "../paraglide/messages.js";

/**
 * What a signed-in account without console access lands on: a plain page, no
 * navigation, no calls to the API. Their `/v1` access (the chat's) is
 * untouched; this only explains why the console shows nothing.
 *
 * The link back is shown only when the deployment said where the chat is
 * (`GATEWAY_CHAT__URL`), and only for an http(s) address.
 */
export function NoConsole({ chatUrl }: { chatUrl?: string }) {
  const link = chatUrl && /^https?:\/\//.test(chatUrl) ? chatUrl : null;
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-xl items-center justify-center p-6">
      <Notice tone="info" title={m["no_console.title"]()}>
        <p>{m["no_console.body"]()}</p>
        {link && (
          <p className="mt-3">
            <a className="font-medium text-accent underline" href={link}>
              {m["no_console.chat_link"]()}
            </a>
          </p>
        )}
      </Notice>
    </div>
  );
}
