import { m } from "../paraglide/messages.js";
import { entityLabel, modeRank } from "./entities";
import type { EntityMode, RedactionPolicy } from "./types";

/**
 * "What this rule does", in one line, computed from the form as it stands.
 *
 * The line is for a person deciding whether the rule says what they meant, so
 * it names a type when one or two share an action ("blocks payment card
 * number") and counts them when more do ("redacts 6 kinds of data"): a list of
 * eleven names is the table the reader just came from.
 *
 * The strongest action comes first, because that is what the reader is
 * checking for. Types set to Off are mentioned only when the default would
 * otherwise have covered them; with the default at Off an "Off" row is the
 * default restated and says nothing.
 */

const STRONGEST_FIRST: EntityMode[] = ["block", "redact", "anonymise", "anonymise_restore"];

/** "Payment card number" becomes "payment card number"; "IBAN" and "URL" stay. */
function lowerFirst(text: string): string {
  const [first = "", second = ""] = text;
  return second !== "" && second === second.toLowerCase() && second !== second.toUpperCase()
    ? first.toLowerCase() + text.slice(1)
    : text;
}

function verbPhrase(mode: EntityMode, what: string): string {
  switch (mode) {
    case "block":
      return m["rule_summary.group.block"]({ what });
    case "redact":
      return m["rule_summary.group.redact"]({ what });
    case "anonymise":
      return m["rule_summary.group.anonymise"]({ what });
    case "anonymise_restore":
      return m["rule_summary.group.anonymise_restore"]({ what });
    case "off":
      return m["rule_summary.group.off"]({ what });
  }
}

function describeTypes(entities: string[]): string {
  if (entities.length <= 2) {
    return entities.map((entity) => lowerFirst(entityLabel(entity))).join(m["rule_summary.and"]());
  }
  return m["rule_summary.kinds"]({ count: entities.length });
}

/**
 * `allowListIgnored` is true when the gateway will not use this rule's
 * allowlist: it takes the list from the first applicable policy alone, which is
 * the catch-all rule when one exists. Saying "never hides 2 values" then would
 * promise protection from redaction the rule does not give.
 */
export function summariseRule(policy: RedactionPolicy, allowListIgnored = false): string {
  const named = Object.entries(policy.entities);
  const parts: string[] = [];

  for (const mode of STRONGEST_FIRST) {
    const types = named.filter(([, entity]) => entity.mode === mode).map(([name]) => name);
    if (types.length > 0) parts.push(verbPhrase(mode, describeTypes(types)));
  }

  if (policy.default_mode !== "off") {
    const rest =
      named.length > 0 ? m["rule_summary.everything_else"]() : m["rule_summary.everything"]();
    parts.push(verbPhrase(policy.default_mode, rest));
    // Types the rule sets back to Off are exceptions to the default, and the
    // reader should hear about them.
    const exceptions = named
      .filter(([, entity]) => modeRank(entity.mode) === 0)
      .map(([name]) => name);
    if (exceptions.length > 0) parts.push(verbPhrase("off", describeTypes(exceptions)));
  }

  if (policy.patterns.length > 0) {
    parts.push(m["rule_summary.patterns"]({ count: policy.patterns.length }));
  }
  if (policy.allow_list.length > 0 && allowListIgnored) {
    parts.push(m["rule_summary.allowed_ignored"]());
  } else if (policy.allow_list.length > 0) {
    parts.push(m["rule_summary.allowed"]({ count: policy.allow_list.length }));
  }

  if (parts.length === 0) return m["rule_summary.empty"]();
  const sentence = parts.join(", ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1) + ".";
}
