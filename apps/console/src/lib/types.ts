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
