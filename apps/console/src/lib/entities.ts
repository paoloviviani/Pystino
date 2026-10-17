import { m } from "../paraglide/messages.js";
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
  /** A message function: this table is built once, at import. */
  label: () => string;
  source: EntitySource;
}

const ENTITIES: Record<string, EntityInfo> = {
  // Read by the language model, so they depend on the prompt's language.
  PERSON: { label: m["entity_type.person"], source: "model" },
  LOCATION: { label: m["entity_type.location"], source: "model" },
  ORGANIZATION: { label: m["entity_type.organization"], source: "model" },
  NRP: { label: m["entity_type.nrp"], source: "model" },

  // Matched by a regex or a checksum, and therefore language-independent.
  CREDIT_CARD: { label: m["entity_type.credit_card"], source: "pattern" },
  CRYPTO: { label: m["entity_type.crypto"], source: "pattern" },
  DATE_TIME: { label: m["entity_type.date_time"], source: "pattern" },
  EMAIL_ADDRESS: { label: m["entity_type.email_address"], source: "pattern" },
  IBAN_CODE: { label: m["entity_type.iban_code"], source: "pattern" },
  IP_ADDRESS: { label: m["entity_type.ip_address"], source: "pattern" },
  PHONE_NUMBER: { label: m["entity_type.phone_number"], source: "pattern" },
  MEDICAL_LICENSE: { label: m["entity_type.medical_license"], source: "pattern" },
  URL: { label: m["entity_type.url"], source: "pattern" },
  IT_FISCAL_CODE: { label: m["entity_type.it_fiscal_code"], source: "pattern" },
  IT_DRIVER_LICENSE: { label: m["entity_type.it_driver_license"], source: "pattern" },
  IT_VAT_CODE: { label: m["entity_type.it_vat_code"], source: "pattern" },
  IT_PASSPORT: { label: m["entity_type.it_passport"], source: "pattern" },
  IT_IDENTITY_CARD: { label: m["entity_type.it_identity_card"], source: "pattern" },
  ES_NIF: { label: m["entity_type.es_nif"], source: "pattern" },
  ES_NIE: { label: m["entity_type.es_nie"], source: "pattern" },
  PL_PESEL: { label: m["entity_type.pl_pesel"], source: "pattern" },
  FI_PERSONAL_IDENTITY_CODE: { label: m["entity_type.fi_personal_identity_code"], source: "pattern" },
  UK_NHS: { label: m["entity_type.uk_nhs"], source: "pattern" },
  UK_NINO: { label: m["entity_type.uk_nino"], source: "pattern" },
  US_BANK_NUMBER: { label: m["entity_type.us_bank_number"], source: "pattern" },
  US_DRIVER_LICENSE: { label: m["entity_type.us_driver_license"], source: "pattern" },
  US_ITIN: { label: m["entity_type.us_itin"], source: "pattern" },
  US_PASSPORT: { label: m["entity_type.us_passport"], source: "pattern" },
  US_SSN: { label: m["entity_type.us_ssn"], source: "pattern" },
  SG_NRIC_FIN: { label: m["entity_type.sg_nric_fin"], source: "pattern" },
  SG_UEN: { label: m["entity_type.sg_uen"], source: "pattern" },
  AU_ABN: { label: m["entity_type.au_abn"], source: "pattern" },
  AU_ACN: { label: m["entity_type.au_acn"], source: "pattern" },
  AU_TFN: { label: m["entity_type.au_tfn"], source: "pattern" },
  AU_MEDICARE: { label: m["entity_type.au_medicare"], source: "pattern" },
  IN_PAN: { label: m["entity_type.in_pan"], source: "pattern" },
  IN_AADHAAR: { label: m["entity_type.in_aadhaar"], source: "pattern" },
  IN_PASSPORT: { label: m["entity_type.in_passport"], source: "pattern" },
  IN_VOTER: { label: m["entity_type.in_voter"], source: "pattern" },
  IN_VEHICLE_REGISTRATION: { label: m["entity_type.in_vehicle_registration"], source: "pattern" },
};

/** The plain-English name, or the raw label when nothing here knows it. */
export function entityLabel(entityType: string): string {
  return ENTITIES[entityType.toUpperCase()]?.label() ?? entityType;
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
export interface Mode {
  value: EntityMode;
  label: () => string;
  short: () => string;
  hint: () => string;
}

export const MODES: Mode[] = [
  {
    value: "off",
    label: m["redaction_mode.off.label"],
    short: m["redaction_mode.off.short"],
    hint: m["redaction_mode.off.hint"],
  },
  {
    value: "anonymise_restore",
    label: m["redaction_mode.anonymise_restore.label"],
    short: m["redaction_mode.anonymise_restore.short"],
    hint: m["redaction_mode.anonymise_restore.hint"],
  },
  {
    value: "anonymise",
    label: m["redaction_mode.anonymise.label"],
    short: m["redaction_mode.anonymise.short"],
    hint: m["redaction_mode.anonymise.hint"],
  },
  {
    value: "redact",
    label: m["redaction_mode.redact.label"],
    short: m["redaction_mode.redact.short"],
    hint: m["redaction_mode.redact.hint"],
  },
  {
    value: "block",
    label: m["redaction_mode.block.label"],
    short: m["redaction_mode.block.short"],
    hint: m["redaction_mode.block.hint"],
  },
];

export function modeLabel(mode: EntityMode | string): string {
  return MODES.find((option) => option.value === mode)?.label() ?? mode;
}

/** Position in the weakest-to-strongest order; -1 for a mode this console does not know. */
export function modeRank(mode: EntityMode | string): number {
  return MODES.findIndex((option) => option.value === mode);
}

/** A policy in one line: the default, and how much is said on top of it. */
/**
 * A policy in one line: the default, and how much is said on top of it.
 *
 * The middot between the parts is a separator between figures rather than a
 * sentence, and is left here rather than inside any one of them — a translator
 * who has to delete a trailing separator inside a message has been asked to
 * carry the separator with them.
 */
export function summarisePolicy(policy: RedactionPolicy): string {
  const parts = [m["policy_summary.default_mode"]({ mode: modeLabel(policy.default_mode) })];
  const entities = Object.keys(policy.entities).length;
  if (entities > 0) parts.push(m["policy_summary.types"]({ count: entities }));
  if (policy.patterns.length > 0) {
    parts.push(m["policy_summary.patterns"]({ count: policy.patterns.length }));
  }
  if (policy.allow_list.length > 0) {
    parts.push(m["policy_summary.allow_list"]({ count: policy.allow_list.length }));
  }
  return parts.join(" · ");
}
