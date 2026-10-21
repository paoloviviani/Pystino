import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import { type Page, pageParams } from "./paging";

/**
 * One administrator change. `changes` maps a field to what it was and what it
 * became; a create has only `after`, a delete only `before`. The API never
 * sends a credential: those fields carry a word such as "rotated".
 */
export interface AuditChange {
  before?: unknown;
  after?: unknown;
}

export interface AuditEvent {
  id: string;
  created_at: string;
  actor_user_id: string | null;
  actor_label: string;
  action: string;
  target_type: string;
  target_id: string | null;
  target_label: string;
  changes: Record<string, AuditChange>;
}

/** Action prefixes the filter offers; the API matches them as prefixes. */
export const AUDIT_ACTION_PREFIXES = [
  "model.",
  "price.",
  "alias.",
  "grant.",
  "provider.",
  "limit.",
] as const;

export const AUDIT_TARGET_TYPES = ["model", "price", "alias", "grant", "provider", "limit"] as const;

export interface AuditQuery {
  /** `YYYY-MM-DD` from a date input, in the reader's own day. */
  from?: string;
  to?: string;
  actor?: string;
  action?: string;
  targetType?: string;
  limit?: number;
  offset?: number;
}

/** The start of the local day, as the instant the API compares against. */
function dayStart(day: string): Date {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year ?? 1970, (month ?? 1) - 1, date ?? 1);
}

/** `to` is a day the reader picked, so it is inclusive: the API's end is exclusive. */
export function auditFilterString(query: AuditQuery): string {
  const params = new URLSearchParams();
  if (query.from) params.set("start", dayStart(query.from).toISOString());
  if (query.to) {
    const end = dayStart(query.to);
    end.setDate(end.getDate() + 1);
    params.set("end", end.toISOString());
  }
  if (query.actor?.trim()) params.set("actor", query.actor.trim());
  if (query.action) params.set("action", query.action);
  if (query.targetType) params.set("target_type", query.targetType);
  return params.toString();
}

export function useAuditLog(query: AuditQuery) {
  const filters = auditFilterString(query);
  const paging = pageParams({ limit: query.limit ?? 50, offset: query.offset ?? 0 });
  return useQuery({
    queryKey: ["admin", "audit", filters, paging],
    queryFn: () => request<Page<AuditEvent>>(`/api/admin/audit?${filters}&${paging}`),
    placeholderData: keepPreviousData,
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status < 500) && failureCount < 2,
  });
}
