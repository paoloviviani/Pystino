import { m } from "../paraglide/messages.js";
import { REDACTION_PATTERN_TEMPLATES } from "./redactionTemplates";
import type {
  EntityMode,
  PlaceholderStyle,
  PrivacyEngines,
  RedactionPolicy,
  UserPolicyEntry,
} from "./types";

/**
 * Example redaction policies, offered as templates and never seeded.
 *
 * A template is a list of items. Choosing one fills a new, unsaved policy that
 * the administrator edits before saving. Nothing is added that the redaction
 * service does not report: a format must be in `pattern_entities`, an entity must
 * be exposed by an available engine, and the ones only GLiNER knows need GLiNER.
 * What is left out is returned as `skipped`, for the picker to say so; it is
 * never dropped silently.
 */
export type TemplateItem =
  | { kind: "format"; name: string; mode: EntityMode; placeholder?: PlaceholderStyle }
  | { kind: "entity"; name: string; mode: EntityMode; gliner?: boolean }
  | { kind: "pattern"; name: string; mode?: EntityMode }
  | { kind: "keywords"; name: string; mode: EntityMode };

export interface PolicyTemplate {
  id: string;
  name: () => string;
  description: () => string;
  items: TemplateItem[];
}

const f = (
  name: string,
  mode: EntityMode,
  placeholder?: PlaceholderStyle,
): TemplateItem => ({ kind: "format", name, mode, ...(placeholder && { placeholder }) });
const e = (name: string, mode: EntityMode, gliner = false): TemplateItem => ({
  kind: "entity",
  name,
  mode,
  gliner,
});
const p = (name: string, mode?: EntityMode): TemplateItem => ({
  kind: "pattern",
  name,
  ...(mode && { mode }),
});

export const POLICY_TEMPLATES: PolicyTemplate[] = [
  {
    id: "secrets",
    name: m["policy_template.secrets.name"],
    description: m["policy_template.secrets.description"],
    items: [
      p("OPENAI_KEY", "redact"),
      p("GITHUB_TOKEN", "redact"),
      p("AWS_ACCESS_KEY_ID", "redact"),
      p("SLACK_TOKEN", "redact"),
      p("BEARER_TOKEN", "redact"),
      p("BEARER_TOKEN_OPAQUE", "redact"),
      p("URL_WITH_PASSWORD", "redact"),
      p("PRIVATE_KEY", "block"),
    ],
  },
  {
    id: "personal",
    name: m["policy_template.personal.name"],
    description: m["policy_template.personal.description"],
    items: [
      f("IBAN_CODE", "redact", "same_length"),
      f("CREDIT_CARD", "redact", "same_length"),
      f("IT_FISCAL_CODE", "anonymise_restore"),
      f("IT_VAT_CODE", "anonymise_restore"),
      f("EMAIL_ADDRESS", "anonymise_restore"),
      f("PHONE_NUMBER", "anonymise_restore"),
      e("PERSON", "anonymise_restore"),
      e("LOCATION", "anonymise_restore"),
    ],
  },
  {
    id: "government",
    name: m["policy_template.government.name"],
    description: m["policy_template.government.description"],
    items: [
      f("IT_IDENTITY_CARD", "redact"),
      f("IT_PASSPORT", "redact"),
      f("IT_DRIVER_LICENSE", "redact"),
      f("ES_NIF", "redact"),
      f("ES_NIE", "redact"),
      f("UK_NINO", "redact"),
      f("UK_NHS", "redact"),
      f("US_SSN", "redact"),
      f("US_PASSPORT", "redact"),
      f("US_DRIVER_LICENSE", "redact"),
      f("US_ITIN", "redact"),
      f("PL_PESEL", "redact"),
    ],
  },
  {
    id: "contact",
    name: m["policy_template.contact.name"],
    description: m["policy_template.contact.description"],
    items: [
      f("EMAIL_ADDRESS", "anonymise_restore"),
      f("PHONE_NUMBER", "anonymise_restore"),
      f("URL", "anonymise_restore"),
    ],
  },
  {
    id: "network",
    name: m["policy_template.network.name"],
    description: m["policy_template.network.description"],
    items: [
      f("IP_ADDRESS", "anonymise"),
      f("MAC_ADDRESS", "anonymise"),
      p("URL_WITH_PASSWORD", "anonymise"),
      { kind: "keywords", name: "INTERNAL_HOSTS", mode: "anonymise" },
    ],
  },
  {
    id: "health",
    name: m["policy_template.health.name"],
    description: m["policy_template.health.description"],
    items: [
      e("HEALTH_INSURANCE_ID", "redact", true),
      e("MEDICAL_CONDITION", "redact", true),
      e("MEDICATION", "redact", true),
    ],
  },
];

/** Why an item was left out. */
export type SkipReason = "service" | "ner" | "gliner" | "not_allowed" | "users";

export interface Skipped {
  reason: SkipReason;
  names: string[];
}

export interface BuiltPolicy {
  policy: RedactionPolicy;
  skipped: Skipped[];
}

function group(skips: [SkipReason, string][]): Skipped[] {
  const out: Skipped[] = [];
  for (const [reason, name] of skips) {
    const held = out.find((s) => s.reason === reason);
    if (held) {
      if (!held.names.includes(name)) held.names.push(name);
    } else out.push({ reason, names: [name] });
  }
  return out;
}

const patternTemplate = (name: string) =>
  REDACTION_PATTERN_TEMPLATES.find((template) => template.name === name);

/**
 * The policy a template makes against what the redaction service reports.
 * `engines` null (not loaded) or unreachable: nothing the service must confirm
 * is added, and the note says so.
 */
export function buildPolicyFromTemplate(
  template: PolicyTemplate,
  engines: PrivacyEngines | null | undefined,
): BuiltPolicy {
  const reachable = engines?.reachable === true;
  const formats = new Set((engines?.pattern_entities ?? []).map((n) => n.toUpperCase()));
  const live = (engines?.engines ?? []).filter((engine) => engine.available);
  const gliner = live.find((engine) => engine.name === "gliner" && engine.entities.length > 0);
  const defaultEngine = live.find((engine) => engine.default && engine.entities.length > 0);
  const policy: RedactionPolicy = {
    default_mode: "off",
    engine: null,
    formats: {},
    patterns: [],
    keywords: [],
    entities: {},
    custom_entities: [],
    allow_list: [],
  };
  const skips: [SkipReason, string][] = [];
  const wantsGliner = template.items.some((i) => i.kind === "entity" && i.gliner);
  // Entities are read by one engine. GLiNER when the template needs it and it is
  // there; otherwise the deployment default, which an unset engine means.
  const engine = wantsGliner && gliner ? gliner : defaultEngine;
  if (wantsGliner && gliner) policy.engine = "gliner";
  const known = new Set((engine?.entities ?? []).map((n) => n.toUpperCase()));

  for (const item of template.items) {
    if (item.kind === "format") {
      if (reachable && formats.has(item.name)) {
        policy.formats[item.name] = {
          mode: item.mode,
          threshold: null,
          ...(item.placeholder && { placeholder: item.placeholder }),
        };
      } else skips.push(["service", item.name]);
    } else if (item.kind === "entity") {
      if (known.has(item.name)) {
        policy.entities[item.name] = { mode: item.mode, threshold: null };
      } else skips.push([item.gliner ? "gliner" : "ner", item.name]);
    } else if (item.kind === "pattern") {
      const source = patternTemplate(item.name);
      if (source) {
        policy.patterns.push({
          name: source.name,
          regex: source.regex,
          mode: item.mode ?? source.mode,
        });
      }
    } else {
      policy.keywords.push({ name: item.name, words: [], case_sensitive: false, mode: item.mode });
    }
  }
  return { policy, skipped: group(skips) };
}

/**
 * The same template as a user's own policy: only the entity types the
 * administrator made available (`allowedTypes`, null: any), no keyword lists, and
 * no type that needs an NER engine when the service has none.
 */
export function buildUserEntries(
  template: PolicyTemplate,
  options: { allowedTypes: string[] | null; nerAvailable: boolean | null },
): { entries: UserPolicyEntry[]; skipped: Skipped[] } {
  const allowed = options.allowedTypes ? new Set(options.allowedTypes.map((t) => t.toUpperCase())) : null;
  const entries: UserPolicyEntry[] = [];
  const skips: [SkipReason, string][] = [];
  for (const item of template.items) {
    if (item.kind === "keywords") {
      skips.push(["users", item.name]);
    } else if (item.kind === "pattern") {
      const source = patternTemplate(item.name);
      if (source) {
        entries.push({
          kind: "pattern",
          name: source.name,
          regex: source.regex,
          mode: item.mode ?? source.mode,
        });
      }
    } else if (item.kind === "entity" && options.nerAvailable === false) {
      skips.push([item.gliner ? "gliner" : "ner", item.name]);
    } else if (allowed && !allowed.has(item.name)) {
      skips.push(["not_allowed", item.name]);
    } else {
      entries.push({ kind: "mode", name: item.name, regex: null, mode: item.mode });
    }
  }
  return { entries, skipped: group(skips) };
}

/** The one-line note for what was left out, or null when nothing was. */
export function skippedNote(skipped: Skipped[]): string | null {
  if (skipped.length === 0) return null;
  return skipped
    .map((s) => {
      const names = s.names.join(", ");
      switch (s.reason) {
        case "ner":
          return m["policy_template.skipped.ner"]({ names });
        case "gliner":
          return m["policy_template.skipped.gliner"]({ names });
        case "service":
          return m["policy_template.skipped.service"]({ names });
        case "users":
          return m["policy_template.skipped.users"]({ names });
        default:
          return m["policy_template.skipped.not_allowed"]({ names });
      }
    })
    .join(" ");
}
