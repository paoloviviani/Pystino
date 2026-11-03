import { ApiError } from "./api";
import { m } from "../paraglide/messages.js";

/**
 * The gateway's refusals specific to bring-your-own-key, in words that say what
 * to do next.
 *
 * The API's own sentence is accurate but written for a client, and for the
 * network guard in particular it names a rule and an address: useful detail,
 * not an explanation. So the explanation leads and the gateway's sentence
 * follows it where it adds something (which rule, which header). Anything not
 * recognised falls through to the API's message untouched.
 */
export function byokErrorText(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  const detail = error.message;
  const code = error instanceof ApiError ? error.code : null;

  switch (code) {
    case "provider_url_blocked":
      return `${m["byok_error.url_blocked"]()} ${detail}`;
    case "byok_not_allowed":
      return m["byok_error.not_allowed"]();
    case "byok_limit_reached":
      return m["byok_error.limit_reached"]();
    case "byok_header_not_allowed":
      return `${m["byok_error.header_not_allowed"]()} ${detail}`;
    case "byok_plugin_not_allowed":
      return m["byok_error.plugin_not_allowed"]();
    case "byok_kind_not_allowed":
      return m["byok_error.kind_not_allowed"]();
    default:
      break;
  }
  // The reserved prefix is refused with no code of its own.
  if (detail.includes("'my/'")) return m["byok_error.reserved_prefix"]();
  return detail;
}
