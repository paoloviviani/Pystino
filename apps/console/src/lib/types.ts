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
  cost: string;
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
export type ModelKind = "chat" | "embedding" | "image";

export interface Price {
  id: string;
  input_per_mtok: string;
  output_per_mtok: string;
  cache_read_per_mtok: string | null;
  cache_write_per_mtok: string | null;
  /** Per generated image, for image models nobody prices per token. */
  per_image: string | null;
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
  issuer: string;
  subject: string;
  is_active: boolean;
  is_admin: boolean;
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
  currency: string | null;
  context_window: number | null;
  /** What the provider claims, shown before importing so the choice is informed. */
  kind: ModelKind;
  input_modalities: string[];
  output_modalities: string[];
  supported_features: string[];
  blocked_reason: string | null;
}

export interface CatalogueDriftRow {
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
  /** Null means every type the engine offers, not none. */
  entity_types: string[] | null;
  timeout_seconds: number;
  cache_size: number;
  /** Whether the HMAC key is set. Never the key. */
  placeholder_key_set: boolean;
  service: RedactionServiceHealth | null;
  activity: RedactionActivity;
  /** Computed by the API, rendered verbatim. */
  warnings: string[];
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
