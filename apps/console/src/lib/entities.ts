import type { EntityMode, RedactionPolicy } from "./types";

/**
 * Plain-English names for the entity types a detector reports.
 *
 * The raw label is always shown beside the name rather than replaced by it: the
 * label is what the policy stores, what the ledger counts and what an operator
 * greps a log for, and a screen that only says "Person name" cannot be matched
 * against any of them.
 *
 * **Unknown labels fall through to the raw label.** An engine gains recognisers
 * between releases (ADR 0026), and a map that hid what it did not recognise
 * would reproduce, one level up, the failure this whole feature exists to fix:
 * a type nobody can see is a type nobody rules on.
 *
 * `source` says where a detection comes from, which is the difference between a
 * type that behaves the same in every language and one that does not: a pattern
 * is a regex and is exact, a model reading is a guess with a score — and it is
 * the guesses that produced the Italian verb read as a person at 0.85.
 */
export type EntitySource = "pattern" | "model";

interface EntityInfo {
  label: string;
  source: EntitySource;
}

const ENTITIES: Record<string, EntityInfo> = {
  // Read by the language model, so they depend on the prompt's language.
  PERSON: { label: "Person name", source: "model" },
  LOCATION: { label: "Place", source: "model" },
  ORGANIZATION: { label: "Organisation", source: "model" },
  NRP: { label: "Nationality, religion or politics", source: "model" },

  // Matched by a regex or a checksum, and therefore language-independent.
  CREDIT_CARD: { label: "Payment card number", source: "pattern" },
  CRYPTO: { label: "Crypto wallet address", source: "pattern" },
  DATE_TIME: { label: "Date or time", source: "pattern" },
  EMAIL_ADDRESS: { label: "Email address", source: "pattern" },
  IBAN_CODE: { label: "Bank account (IBAN)", source: "pattern" },
  IP_ADDRESS: { label: "IP address", source: "pattern" },
  PHONE_NUMBER: { label: "Phone number", source: "pattern" },
  MEDICAL_LICENSE: { label: "Medical licence number", source: "pattern" },
  URL: { label: "Web address", source: "pattern" },
  IT_FISCAL_CODE: { label: "Italian tax code", source: "pattern" },
  IT_DRIVER_LICENSE: { label: "Italian driving licence", source: "pattern" },
  IT_VAT_CODE: { label: "Italian VAT number", source: "pattern" },
  IT_PASSPORT: { label: "Italian passport number", source: "pattern" },
  IT_IDENTITY_CARD: { label: "Italian identity card", source: "pattern" },
  ES_NIF: { label: "Spanish tax number", source: "pattern" },
  ES_NIE: { label: "Spanish foreigner number", source: "pattern" },
  PL_PESEL: { label: "Polish national number", source: "pattern" },
  FI_PERSONAL_IDENTITY_CODE: { label: "Finnish personal identity code", source: "pattern" },
  UK_NHS: { label: "NHS number", source: "pattern" },
  UK_NINO: { label: "UK national insurance number", source: "pattern" },
  US_BANK_NUMBER: { label: "US bank account", source: "pattern" },
  US_DRIVER_LICENSE: { label: "US driving licence", source: "pattern" },
  US_ITIN: { label: "US taxpayer number", source: "pattern" },
  US_PASSPORT: { label: "US passport number", source: "pattern" },
  US_SSN: { label: "US social security number", source: "pattern" },
  SG_NRIC_FIN: { label: "Singapore NRIC or FIN", source: "pattern" },
  SG_UEN: { label: "Singapore entity number", source: "pattern" },
  AU_ABN: { label: "Australian business number", source: "pattern" },
  AU_ACN: { label: "Australian company number", source: "pattern" },
  AU_TFN: { label: "Australian tax file number", source: "pattern" },
  AU_MEDICARE: { label: "Australian Medicare number", source: "pattern" },
  IN_PAN: { label: "Indian PAN", source: "pattern" },
  IN_AADHAAR: { label: "Indian Aadhaar number", source: "pattern" },
  IN_PASSPORT: { label: "Indian passport number", source: "pattern" },
  IN_VOTER: { label: "Indian voter ID", source: "pattern" },
  IN_VEHICLE_REGISTRATION: { label: "Indian vehicle registration", source: "pattern" },
};

/** The plain-English name, or the raw label when nothing here knows it. */
export function entityLabel(entityType: string): string {
  return ENTITIES[entityType.toUpperCase()]?.label ?? entityType;
}

/**
 * Where a detection comes from, or null when it is not known.
 *
 * Null rather than a guess: a custom pattern is passed in by the caller, and
 * anything else unrecognised belongs to an engine this console has never heard
 * of. Claiming a source for it would be inventing a fact about someone else's
 * recogniser.
 */
export function entitySource(
  entityType: string,
  customPatternNames: readonly string[] = [],
): EntitySource | null {
  const name = entityType.toUpperCase();
  if (customPatternNames.some((pattern) => pattern.toUpperCase() === name)) return "pattern";
  return ENTITIES[name]?.source ?? null;
}

/**
 * The modes, weakest to strongest.
 *
 * The order is the API's own (`EntityMode`), and it is load-bearing twice: it is
 * the order these are listed in, and it is what "may only tighten" compares by.
 */
export const MODES: { value: EntityMode; label: string; short: string; hint: string }[] = [
  { value: "off", label: "Not redacted", short: "Off", hint: "left exactly as the caller wrote it" },
  {
    value: "anonymise_restore",
    label: "Anonymise, restore in the answer",
    short: "Restore",
    hint: "placeholder upstream, real value back to the reader",
  },
  {
    value: "anonymise",
    label: "Anonymise",
    short: "Anonymise",
    hint: "placeholder upstream and in the answer",
  },
  {
    value: "redact",
    label: "Redact",
    short: "Redact",
    hint: "<PERSON>, so two people look the same",
  },
  {
    value: "block",
    label: "Block",
    short: "Block",
    hint: "the request is refused before a provider sees it",
  },
];

export function modeLabel(mode: EntityMode | string): string {
  return MODES.find((option) => option.value === mode)?.label ?? mode;
}

/** Position in the weakest-to-strongest order; -1 for a mode this console does not know. */
export function modeRank(mode: EntityMode | string): number {
  return MODES.findIndex((option) => option.value === mode);
}

/** A policy in one line: the default, and how much is said on top of it. */
export function summarisePolicy(policy: RedactionPolicy): string {
  const parts = [`${modeLabel(policy.default_mode)} by default`];
  const entities = Object.keys(policy.entities).length;
  if (entities > 0) parts.push(`${entities} ${entities === 1 ? "type" : "types"}`);
  if (policy.patterns.length > 0) {
    parts.push(
      `${policy.patterns.length} ${policy.patterns.length === 1 ? "pattern" : "patterns"}`,
    );
  }
  if (policy.allow_list.length > 0) parts.push(`${policy.allow_list.length} allowed`);
  return parts.join(" · ");
}
