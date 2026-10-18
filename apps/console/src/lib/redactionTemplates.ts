import { m } from "../paraglide/messages.js";
import type { EntityMode } from "./types";

/**
 * Selectable custom-pattern templates for a redaction policy.
 *
 * Templates are offered, never seeded. Seeding the credential shapes into
 * every new rule made an empty pattern list impossible to distinguish from a
 * deliberate decision not to cover credentials. A template added here is an
 * explicit choice, and it remains editable after it is added.
 *
 * Every expression must stay within RE2: no backreferences and no lookaround.
 * The API validates with RE2 before saving, but a template that cannot be
 * saved should never be offered.
 */
export interface RedactionPatternTemplate {
  /** The pattern's own name, as it is stored and as it appears in a log. */
  name: string;
  regex: string;
  mode: EntityMode;
  kind: "credential" | "identifier";
  /** A message function: the catalogue is built once, at import. */
  description: () => string;
}

export const REDACTION_PATTERN_TEMPLATES: RedactionPatternTemplate[] = [
  {
    name: "OPENAI_KEY",
    regex: "sk-[A-Za-z0-9_-]{16,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.openai_key.description"],
  },
  {
    name: "AWS_ACCESS_KEY_ID",
    regex: "A(KIA|SIA)[0-9A-Z]{16}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.aws_access_key_id.description"],
  },
  {
    name: "GITHUB_TOKEN",
    regex: "gh[pousr]_[A-Za-z0-9]{36,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.github_token.description"],
  },
  {
    name: "GITHUB_FINE_GRAINED_TOKEN",
    regex: "github_pat_[A-Za-z0-9_]{22,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.github_fine_grained_token.description"],
  },
  {
    name: "GITLAB_TOKEN",
    regex: "glpat-[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.gitlab_token.description"],
  },
  {
    name: "SLACK_TOKEN",
    regex: "xox[baprs]-[A-Za-z0-9-]{10,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.slack_token.description"],
  },
  {
    name: "GOOGLE_API_KEY",
    regex: "AIza[0-9A-Za-z_-]{35}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.google_api_key.description"],
  },
  {
    name: "STRIPE_SECRET_KEY",
    regex: "s[kt]_(live|test)_[0-9A-Za-z]{24,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.stripe_secret_key.description"],
  },
  {
    name: "HUGGING_FACE_TOKEN",
    regex: "hf_[A-Za-z0-9]{20,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.hugging_face_token.description"],
  },
  {
    name: "TELEGRAM_BOT_TOKEN",
    regex: "[0-9]{8,10}:[A-Za-z0-9_-]{30,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.telegram_bot_token.description"],
  },
  {
    name: "SENDGRID_API_KEY",
    regex: "SG\\.[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.sendgrid_api_key.description"],
  },
  {
    name: "NPM_TOKEN",
    regex: "npm_[A-Za-z0-9_-]{32,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.npm_token.description"],
  },
  {
    name: "PYPI_TOKEN",
    regex: "pypi-[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.pypi_token.description"],
  },
  {
    name: "PRIVATE_KEY",
    regex: "-----BEGIN [A-Z ]*PRIVATE KEY-----",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.private_key.description"],
  },
  {
    name: "BEARER_TOKEN",
    regex: "eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.bearer_token.description"],
  },
  {
    name: "BEARER_TOKEN_OPAQUE",
    regex: "[Bb][Ee][Aa][Rr][Ee][Rr] +[A-Za-z0-9_.~+\\/-]{20,}={0,2}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.bearer_token_opaque.description"],
  },
  {
    name: "BASIC_AUTH_HEADER",
    regex: "[Bb][Aa][Ss][Ii][Cc] +[A-Za-z0-9+/]{20,}={0,2}",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.basic_auth_header.description"],
  },
  {
    name: "URL_WITH_PASSWORD",
    regex: "[A-Za-z][A-Za-z0-9+.-]*://[^/\\s:@]+:[^/\\s@]+@[^/\\s]+",
    mode: "block",
    kind: "credential",
    description: m["pattern_template.url_with_password.description"],
  },
  {
    name: "UUID",
    regex: "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    mode: "redact",
    kind: "identifier",
    description: m["pattern_template.uuid.description"],
  },
  {
    name: "INTERNAL_TICKET",
    regex: "[A-Z]{2,8}-[0-9]{3,8}",
    mode: "redact",
    kind: "identifier",
    description: m["pattern_template.internal_ticket.description"],
  },
  {
    name: "DATE_OF_BIRTH",
    regex: "\\b(0?[1-9]|[12][0-9]|3[01])[/.-](0?[1-9]|1[0-2])[/.-](19|20)[0-9]{2}\\b",
    mode: "redact",
    kind: "identifier",
    description: m["pattern_template.date_of_birth.description"],
  },
];
