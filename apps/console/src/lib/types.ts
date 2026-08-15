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

// -- administration ----------------------------------------------------------
//
// Decimals are strings here for the same reason they are above: the gateway
// serialises `Numeric` as a string so no float ever touches money, and typing
// them as `number` would undo that at the last hop.

export interface Price {
  id: string;
  input_per_mtok: string;
  output_per_mtok: string;
  cache_read_per_mtok: string | null;
  cache_write_per_mtok: string | null;
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
  /** "chat" or "embedding": which /v1 route may use it. */
  kind: "chat" | "embedding";
  display_name: string | null;
  description: string | null;
  is_active: boolean;
  context_window: number | null;
  max_output_tokens: number | null;
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
  /** Whether the gateway adds `stream_options.include_usage` to streaming calls. */
  forward_stream_options: boolean;
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
