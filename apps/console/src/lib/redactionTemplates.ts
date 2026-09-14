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
  name: string;
  regex: string;
  mode: EntityMode;
  kind: "credential" | "identifier";
  description: string;
}

export const REDACTION_PATTERN_TEMPLATES: RedactionPatternTemplate[] = [
  {
    name: "OPENAI_KEY",
    regex: "sk-[A-Za-z0-9_-]{16,}",
    mode: "block",
    kind: "credential",
    description: "OpenAI-style secret key.",
  },
  {
    name: "AWS_ACCESS_KEY_ID",
    regex: "A(KIA|SIA)[0-9A-Z]{16}",
    mode: "block",
    kind: "credential",
    description: "AWS access-key identifier.",
  },
  {
    name: "GITHUB_TOKEN",
    regex: "gh[pousr]_[A-Za-z0-9]{36,}",
    mode: "block",
    kind: "credential",
    description: "GitHub token with a published prefix.",
  },
  {
    name: "GITHUB_FINE_GRAINED_TOKEN",
    regex: "github_pat_[A-Za-z0-9_]{22,}",
    mode: "block",
    kind: "credential",
    description: "GitHub fine-grained personal access token.",
  },
  {
    name: "GITLAB_TOKEN",
    regex: "glpat-[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: "GitLab personal access token.",
  },
  {
    name: "SLACK_TOKEN",
    regex: "xox[baprs]-[A-Za-z0-9-]{10,}",
    mode: "block",
    kind: "credential",
    description: "Slack token with a published prefix.",
  },
  {
    name: "GOOGLE_API_KEY",
    regex: "AIza[0-9A-Za-z_-]{35}",
    mode: "block",
    kind: "credential",
    description: "Google API key.",
  },
  {
    name: "STRIPE_SECRET_KEY",
    regex: "s[kt]_(live|test)_[0-9A-Za-z]{24,}",
    mode: "block",
    kind: "credential",
    description: "Stripe live or test secret key.",
  },
  {
    name: "HUGGING_FACE_TOKEN",
    regex: "hf_[A-Za-z0-9]{20,}",
    mode: "block",
    kind: "credential",
    description: "Hugging Face access token.",
  },
  {
    name: "TELEGRAM_BOT_TOKEN",
    regex: "[0-9]{8,10}:[A-Za-z0-9_-]{30,}",
    mode: "block",
    kind: "credential",
    description: "Telegram bot token.",
  },
  {
    name: "SENDGRID_API_KEY",
    regex: "SG\\.[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: "SendGrid API key.",
  },
  {
    name: "NPM_TOKEN",
    regex: "npm_[A-Za-z0-9_-]{32,}",
    mode: "block",
    kind: "credential",
    description: "npm access token.",
  },
  {
    name: "PYPI_TOKEN",
    regex: "pypi-[A-Za-z0-9_-]{20,}",
    mode: "block",
    kind: "credential",
    description: "PyPI API token.",
  },
  {
    name: "PRIVATE_KEY",
    regex: "-----BEGIN [A-Z ]*PRIVATE KEY-----",
    mode: "block",
    kind: "credential",
    description: "PEM private-key header.",
  },
  {
    name: "BEARER_TOKEN",
    regex: "eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}",
    mode: "block",
    kind: "credential",
    description: "JWT by shape: three dot-separated base64url segments.",
  },
  {
    name: "BEARER_TOKEN_OPAQUE",
    regex: "[Bb][Ee][Aa][Rr][Ee][Rr] +[A-Za-z0-9_.~+\\/-]{20,}={0,2}",
    mode: "block",
    kind: "credential",
    description: "Authorization header carrying a non-JWT bearer token.",
  },
  {
    name: "BASIC_AUTH_HEADER",
    regex: "[Bb][Aa][Ss][Ii][Cc] +[A-Za-z0-9+/]{20,}={0,2}",
    mode: "block",
    kind: "credential",
    description: "HTTP Basic credential.",
  },
  {
    name: "URL_WITH_PASSWORD",
    regex: "[A-Za-z][A-Za-z0-9+.-]*://[^/\\s:@]+:[^/\\s@]+@[^/\\s]+",
    mode: "block",
    kind: "credential",
    description: "URL with an embedded password.",
  },
  {
    name: "UUID",
    regex: "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    mode: "redact",
    kind: "identifier",
    description: "Universally unique identifier, often a session or correlation ID.",
  },
  {
    name: "INTERNAL_TICKET",
    regex: "[A-Z]{2,8}-[0-9]{3,8}",
    mode: "redact",
    kind: "identifier",
    description: "Short prefixed ticket or issue reference.",
  },
  {
    name: "DATE_OF_BIRTH",
    regex: "\\b(0?[1-9]|[12][0-9]|3[01])[/.-](0?[1-9]|1[0-2])[/.-](19|20)[0-9]{2}\\b",
    mode: "redact",
    kind: "identifier",
    description: "Calendar date in day-month-year order.",
  },
];
