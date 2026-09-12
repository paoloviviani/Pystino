/**
 * Wire types, mirroring `gateway.schemas`.
 *
 * Hand-written rather than generated from the OpenAPI document. Generation is
 * the better answer once the console covers the whole admin surface — it is on
 * the list for the remaining screens — but a generator plus its config is a poor
 * trade for the four responses this slice reads, and hand-writing them forces a
 * look at each field rather than importing a hundred unused ones.
 *
 * **Money is a string everywhere.** The gateway stores `Numeric(24,12)` and
 * serialises decimals as strings precisely so no float ever touches an amount.
 * Typing these as `number` would undo that at the last hop.
 */

export interface GroupSummary {
  id: string;
  name: string;
  description: string | null;
}

export interface Me {
  id: string;
  email: string | null;
  display_name: string | null;
  is_admin: boolean;
  groups: GroupSummary[];
  default_billing_group: GroupSummary | null;
  /** "local" means a password here is the way in; anything else is the IdP's
   * (ADR 0049) — the account menu offers password management on that basis. */
  issuer: string;
  has_password: boolean;
}

/** A self-service password change: the current one proves the person. */
export interface MyPasswordChangeInput {
  current_password: string;
  new_password: string;
}

export interface PeriodInfo {
  label: string;
  kind: string | null;
  start: string;
  end: string;
  timezone: string;
}

export interface UsageReportRow {
  key: string | null;
  label: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** Pictures generated. Non-zero only for image models, which often bill per image. */
  images: number;
  searches: number;
  /** Ours, against our own backends: counted, never priced. */
  own_searches: number;
  cost: string;
  /** The native figure — what the model's price table produced, in its own
   * currency (ADR 0054). Present on model rows only. */
  native_cost: string | null;
  native_currency: string | null;
  estimated_requests: number;
  unavailable_requests: number;
}

export interface UsageReport {
  period: PeriodInfo;
  group_by: string;
  currency: string;
  rows: UsageReportRow[];
  totals: UsageReportRow;
  /** Plain-language caveats. Rendered verbatim — see the Notice component. */
  disclosures: string[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  billing_group: GroupSummary | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

/**
 * What minting returns, exactly once.
 *
 * `secret` exists on this response and nowhere else — the gateway stores only a
 * hash, so it cannot be re-read, re-sent or recovered. A separate type rather
 * than an optional field on `ApiKey`, so that anywhere holding a plain `ApiKey`
 * provably has no secret in it.
 */
export interface MintedApiKey extends ApiKey {
  secret: string;
}

// -- administration ----------------------------------------------------------
//
// Decimals are strings here for the same reason they are above: the gateway
// serialises `Numeric` as a string so no float ever touches money, and typing
// them as `number` would undo that at the last hop.

/** What a model produces, and therefore which route may use it. */
export type ModelKind = "chat" | "embedding" | "image" | "ocr" | "search";

export interface Price {
  id: string;
  input_per_mtok: string;
  output_per_mtok: string;
  cache_read_per_mtok: string | null;
  cache_write_per_mtok: string | null;
  /** Per generated image, for image models nobody prices per token. */
  per_image: string | null;
  /** Per page read, for OCR models, whose token rates are usually zero. */
  per_page: string | null;
  /** Per provider-side web search, charged on top of tokens (ADR 0058). */
  per_search: string | null;
  currency: string;
  effective_from: string;
  source: string;
}

export interface AdminModel {
  id: string;
  name: string;
  upstream_model: string;
  provider_id: string;
  provider_name: string;
  /** A model behind a deactivated provider is unreachable, and says so. */
  provider_is_active: boolean;
  /** Which /v1 route may serve it. */
  kind: ModelKind;
  display_name: string | null;
  description: string | null;
  is_active: boolean;
  /** Public access (ADR 0045): any authenticated caller, billed to their own group. */
  is_public: boolean;
  context_window: number | null;
  max_output_tokens: number | null;
  /** What it accepts, produces and can do. Empty means "nobody has said". */
  input_modalities: string[];
  output_modalities: string[];
  supported_features: string[];
  created_at: string;
  current_price: Price | null;
  granted_to: string[];
  /** Users granted this model personally, on top of their groups. */
  granted_to_users: string[];
}

export interface AdminGroup {
  id: string;
  name: string;
  description: string | null;
  source: string;
  is_active: boolean;
  member_count: number;
  models: string[];
}

export interface AdminUser {
  id: string;
  email: string | null;
  display_name: string | null;
  /** The directory's own name for this person (`preferred_username`).
   *
   * Distinct from `display_name`: this is what the account was *created as*,
   * and therefore what an administrator searches for. Null for a local
   * account, and null for a directory account that has not signed in since
   * the gateway started recording it — the claim arrives with a login. */
  username: string | null;
  issuer: string;
  subject: string;
  is_active: boolean;
  is_admin: boolean;
  /** The account can sign in with a password (local accounts, ADR 0043). */
  has_password: boolean;
  /** Directories that also name this account, by issuer (ADR 0056). A linked
   * account has two doors, and one of them is not on this screen otherwise. */
  linked_identities: string[];
  /** Who granted this person's membership of the group being listed (ADR 0057).
   * Only the group-members listing answers it; null everywhere else. */
  membership_source: "manual" | "oidc" | null;
  groups: string[];
  default_billing_group: string | null;
  active_key_count: number;
  last_login_at: string | null;
}

export interface LimitRule {
  id: string;
  name: string;
  scope: "global" | "group" | "user" | "api_key";
  scope_id: string | null;
  metric: "requests" | "tokens" | "cost";
  window_seconds: number | null;
  period: string | null;
  /** "3600s" or "month" — one string to print, whichever kind of rule it is. */
  window_label: string;
  limit_value: string;
  is_active: boolean;
  /** Live counter value. Absent — not zero — when the counter store is unreachable. */
  current_value: string | null;
  last_reset_at: string | null;
}

export interface QuotaReset {
  id: string;
  rule_id: string;
  effective_at: string;
  reason: string;
  created_by: string | null;
  created_by_email: string | null;
}

export interface DiscoveredModel {
  upstream_model: string;
  suggested_name: string;
  input_per_mtok: string | null;
  output_per_mtok: string | null;
  /** Per page, for an OCR model — whose token rates are zero and whose real
   * price is this one. Shown before adopting, like the token rates. */
  per_page: string | null;
  currency: string | null;
  context_window: number | null;
  /** What the provider claims, shown before importing so the choice is informed. */
  kind: ModelKind;
  input_modalities: string[];
  output_modalities: string[];
  supported_features: string[];
  blocked_reason: string | null;
  /**
   * Who supplied the figures above: `provider` for the counterparty's own
   * catalogue, `community` for a gap filled from LiteLLM, null for a model the
   * provider lists and nobody prices. Per row, because one import can mix all
   * three (ADR 0053).
   */
  price_source: "provider" | "community" | null;
}

export interface CatalogueDriftRow {
  /** So the drift warning can link to the model it is about. */
  id: string;
  name: string;
  upstream_model: string;
  is_active: boolean;
}

export interface CatalogueDiscovery {
  provider_url: string;
  provider_model_count: number;
  available: DiscoveredModel[];
  catalogued: CatalogueDriftRow[];
  /** Ours, no longer offered upstream. The drift that breaks at 3am. */
  missing_upstream: CatalogueDriftRow[];
  unparsable: string[];
}

export interface ModelImportResult {
  upstream_model: string;
  name: string | null;
  imported: boolean;
  priced: boolean;
  reason: string | null;
  /** Who supplied the price that was written to the append-only history. */
  price_source: "provider" | "community" | null;
}

export interface ModelImportResponse {
  results: ModelImportResult[];
}

/** What the detection service says about itself, asked at read time. */
export interface RedactionServiceHealth {
  reachable: boolean;
  detail: string;
  latency_ms: number | null;
  engine: string | null;
  engine_version: string | null;
  languages: string[];
  models: Record<string, string>;
  /** Served without an NER model, so fewer entities are found. */
  degraded_languages: string[];
  entities: string[];
}

export interface RedactionActivity {
  window_seconds: number;
  requests: number;
  requests_redacted: number;
  entities_redacted: number;
  engines: string[];
}

/**
 * One installed engine, as something an admin can choose.
 *
 * Read from the API rather than hardcoded, so installing an engine through the
 * `llmp.redactors` entry point makes it selectable without a console release —
 * the same argument the provider-type list makes (ADR 0026, ADR 0033).
 */
export interface RedactionEngineOption {
  name: string;
  label: string;
  description: string;
  /** Whether it calls a detection service. */
  needs_endpoint: boolean;
  /**
   * Whether it removes anything at all. `noop` is a real recorded engine rather
   * than an absence, so this cannot be derived from the name without hardcoding
   * that name here.
   */
  redacts: boolean;
  is_active: boolean;
  /** Null when it can be enabled; otherwise why not, computed server-side. */
  blocked_reason: string | null;
}

/** Who last changed the engine, when, and why. Null when the environment decides. */
export interface RedactionConfigChange {
  engine: string;
  reason: string;
  changed_at: string;
  /** Null once the user has been erased — the record of the change outlives them. */
  changed_by: string | null;
}

/** The redaction layer as the gateway is actually running it (ADR 0012, ADR 0033). */
/**
 * What happens to one kind of detected entity (ADR 0037).
 *
 * Two questions, not one: what the model sees, and what the reader gets back.
 * Ordered weakest to strongest, which is the order the screen lists them.
 */
export type EntityMode = "off" | "anonymise_restore" | "anonymise" | "redact" | "block";

export interface EntityPolicy {
  mode: EntityMode;
  /** Overrides the global score threshold for this type. Null means it applies. */
  threshold: number | null;
}

/**
 * A regex an operator wrote, treated as one more entity type.
 *
 * Compiled with RE2 in the gateway, so a pattern is validated there and not
 * here: RE2 refuses constructs JavaScript accepts — backreferences, lookaround —
 * and a browser-side check would pass a pattern the API then rejects.
 */
export interface CustomPattern {
  name: string;
  regex: string;
  mode: EntityMode;
}

export interface RedactionPolicy {
  /** What happens to a type nobody has ruled on. Defaults to protecting it. */
  default_mode: EntityMode;
  entities: Record<string, EntityPolicy>;
  /** Unioned across scopes: a pattern can only find more, never less. */
  patterns: CustomPattern[];
  /** Values never redacted, whatever the detector says. Matched exactly. */
  allow_list: string[];
}

export interface RedactionStatus {
  engine: string;
  enabled: boolean;
  endpoint: string | null;
  installed_engines: string[];
  engines: RedactionEngineOption[];
  /** `console` when a stored decision is in force, `environment` otherwise. */
  source: "console" | "environment";
  configured: RedactionConfigChange | null;
  /**
   * How long another worker may still run the previous engine after a change.
   * Reported rather than implied: a change that looks instant and is not is
   * worse than one that says how long it takes.
   */
  propagation_seconds: number;
  fail_open: boolean;
  restore_in_response: boolean;
  language: string;
  score_threshold: number;
  /** Null means every type the engine offers, not none. Superseded by `policy`. */
  entity_types: string[] | null;
  /** What is acted on and how, per entity type. */
  policy: RedactionPolicy;
  /**
   * `console` when the row in force carries a policy. Separate from `source`
   * because a row may set the engine and say nothing about the policy.
   */
  policy_source: "console" | "environment";
  timeout_seconds: number;
  cache_size: number;
  /** Whether the HMAC key is set. Never the key. */
  placeholder_key_set: boolean;
  service: RedactionServiceHealth | null;
  activity: RedactionActivity;
  /** Computed by the API, rendered verbatim. */
  warnings: string[];
}

/** The five subjects a redaction rule can name (ADR 0038). */
export type RedactionScope = "all" | "provider" | "model" | "group" | "user" | "api_key";

export interface RedactionRule {
  id: string;
  name: string;
  scope: RedactionScope;
  /** Null for the catch-all scope, whose subject is every request. */
  scope_id: string | null;
  /**
   * The model, provider or group name, the user's email, the key's prefix.
   * **Null means the subject no longer exists**, and a rule pointing at a
   * deleted subject is inert while looking identical to a working one.
   */
  subject_label: string | null;
  policy: RedactionPolicy;
  is_active: boolean;
  reason: string;
  created_by: string | null;
  /** Null once the account is erased; the rule outlives its author. */
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
}

/** One thing the detector found in a sample, and what the policy said about it. */
export interface RedactionPreviewSpan {
  entity_type: string;
  start: number;
  end: number;
  score: number;
  mode: EntityMode;
  threshold: number;
  allow_listed: boolean;
}

export interface RedactionPreview {
  engine: string;
  /** The narrowest rule that contributed. Null when only the deployment policy applied. */
  scope: RedactionScope | null;
  rule_id: string | null;
  policy: RedactionPolicy;
  spans: RedactionPreviewSpan[];
  /** What the provider would receive. Null when the request would be blocked. */
  redacted_text: string | null;
  entity_count: number;
  blocked: boolean;
  blocked_reason: string | null;
  /** Set when the engine in force detects nothing, so an empty result is not read as "clean". */
  note: string | null;
}

/**
 * One quota rule that constrains the person signed in.
 *
 * Narrower than {@link LimitRule} on purpose. No `scope_id`, because the only
 * scopes here are global, this user, or a group they are in — an id would name
 * either themselves or something they already know. No `is_active`, because an
 * inactive rule is not returned at all.
 */
export interface MyLimit {
  id: string;
  name: string;
  scope: string;
  metric: string;
  window_label: string;
  limit_value: string;
  /**
   * Absent, not zero, when the counter store cannot be reached. A budget drawn
   * as untouched because Valkey is down is worse than one drawn as unknown.
   */
  current_value: string | null;
  /** The caller's own notification thresholds (ADR 0052). */
  notification_thresholds?: number[];
}

/**
 * An installed provider type.
 *
 * The plugin *is* the type as far as an operator is concerned: it decides how
 * the counterparty is talked to and what may be believed about what it charged.
 * Read from the API rather than hardcoded, so installing a plugin makes it
 * selectable without a console release (ADR 0032).
 */
export interface ProviderPlugin {
  name: string;
  label: string;
  description: string;
  /** `provider` implies the serving endpoint; `router` chooses it per request. */
  kind: "provider" | "router";
  /** Only the modes this plugin can support, so the UI cannot offer a refusal. */
  billing_modes: ("own_prices" | "provider_reported")[];
  /**
   * The counterparty's public endpoint, when it has one worth pre-filling —
   * choosing the Cortecs type is the act of choosing its endpoint. Null for a
   * type whose endpoints vary (generic, self-hosted).
   */
  default_base_url: string | null;
  is_default: boolean;
}

export interface AdminProvider {
  id: string;
  name: string;
  description: string | null;
  base_url: string;
  /** Masked. The key itself is never returned by the API (ADR 0027). */
  api_key_hint: string;
  has_api_key: boolean;
  extra_headers: Record<string, string>;
  is_active: boolean;
  /**
   * Which plugin carries this counterparty's quirks. Null is the default type.
   *
   * It replaced three fields that were each added for one provider's habit —
   * `auth_scheme`, `forward_stream_options` and `upstream_cost_unit`. The type
   * now decides all three, which is why the dialog asks for one selection
   * instead of four (ADR 0032).
   */
  plugin: string | null;
  kind: "provider" | "router";
  /** What the named plugin actually is, so a mismatch with `kind` is visible. */
  plugin_kind: string | null;
  /** Whose figure is the charge. */
  billing_mode: "own_prices" | "provider_reported";
  /**
   * Active models here with no price row. They reserve nothing, so no cost
   * ceiling ever trips for them — a hole in any mode and a sharp one in
   * pass-through, where the provider's figure arrives too late to admit on.
   */
  unpriced_model_count: number;
  /** How many models this provider serves — the blast radius of turning it off. */
  model_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProviderTestResult {
  ok: boolean;
  status_code: number | null;
  detail: string;
  model_count: number | null;
  sample: string[];
  latency_ms: number | null;
}

/**
 * Minting a local account from the console (ADR 0048). Local only: an
 * identity-provider account is the IdP's to create, and one made here would
 * be overwritten or orphaned at the next login.
 */
export interface UserCreateInput {
  email: string;
  password: string;
  display_name?: string;
  is_admin?: boolean;
  /** Group *names*; a name that does not exist yet is created (source "manual"). */
  groups?: string[];
}

/**
 * The identity policy in force (ADR 0048), as `GET /api/admin/oidc/policy`
 * reports it: the effective values, where each came from, and the newest
 * decision behind them.
 */
export interface OidcPolicy {
  auto_provision: boolean;
  unknown_user_policy: "refuse" | "create_inactive";
  groups_claim: string;
  group_mappings: { idp: string; local: string }[];
  source: "console" | "environment";
  /** Which fields a stored row decides; the rest are the environment's. */
  sources: Record<string, string>;
  configured: {
    reason: string;
    changed_at: string;
    changed_by: string | null;
  } | null;
  propagation_seconds: number;
}

/**
 * A policy change. Unset fields store as null — "the environment decides this
 * one" — so a change to one knob never restates the deployment's other
 * answers.
 */
export interface OidcPolicyInput {
  auto_provision?: boolean;
  unknown_user_policy?: "refuse" | "create_inactive";
  groups_claim?: string;
  group_mappings?: { idp: string; local: string }[];
  reason?: string;
}

/** Creating a group (ADR 0050). Manual is the point: it is the one kind whose
 * membership the console may edit. */
export interface GroupCreateInput {
  name: string;
  description?: string;
}

/** The policy in force (ADR 0049): a reset link is only minted when the
 * deployment enabled the feature. */
export interface PasswordResetEnabled {
  local: boolean;
  reset_available: boolean;
}

// -- Settings (ADR 0051) ------------------------------------------------------

export type GroupSync = "every_login" | "first_login" | "never";

/** One configured identity provider. The client secret is write-only: a
 * response never carries it, only the fact that one is stored. */
export interface IdentityProvider {
  id: string;
  name: string;
  issuer: string;
  client_id: string;
  has_client_secret: boolean;
  scopes: string[];
  groups_claim: string;
  fetch_userinfo: boolean;
  group_mappings: { idp: string; local: string }[];
  /** Whether a login here may adopt the local account with the same verified
   * address (ADR 0056). */
  link_local_by_email: boolean;
  /** How far this directory's answer about groups reaches (ADR 0057). It never
   * reaches a membership an administrator granted, in any of the three. */
  group_sync: GroupSync;
  is_enabled: boolean;
  source: "console" | "environment";
}

export interface IdentityProviderInput {
  name?: string;
  issuer?: string;
  client_id?: string;
  client_secret?: string;
  scopes?: string[];
  groups_claim?: string;
  fetch_userinfo?: boolean;
  group_mappings?: { idp: string; local: string }[];
  link_local_by_email?: boolean;
  group_sync?: GroupSync;
  is_enabled?: boolean;
}

/** The SMTP configuration in force — the row's, or the environment's. */
export interface EmailSettings {
  host: string;
  port: number;
  username: string;
  from_address: string;
  has_password: boolean;
  source: "console" | "environment";
  enabled: boolean;
}

export interface EmailSettingsInput {
  host: string;
  port: number;
  username?: string;
  /** Write-only. Omitted means "keep the stored one". */
  password?: string;
  from_address: string;
}

export interface EmailTestResult {
  ok: boolean;
  detail: string;
}

/** The sign-in page's menu, from GET /auth/methods (ADR 0051). */
export interface AuthMethods {
  local: boolean;
  oidc: boolean;
  providers: { name: string; issuer: string }[];
}

// -- Quota notifications (ADR 0052) -------------------------------------------

/** The caller's thresholds for one rule: percentages, arbitrary, user-owned. */
export interface MyNotificationThresholdsInput {
  thresholds: number[];
}

