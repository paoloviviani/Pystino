import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import { type Page, pageParams } from "./paging";

/**
 * One line of the request log: metadata only. The API sends no prompt,
 * completion or error text, so there is nothing here to render by accident.
 */
export interface RequestLogRow {
  request_id: string;
  created_at: string;
  user_id: string | null;
  user_label: string | null;
  group_id: string | null;
  group_name: string | null;
  requested_model: string | null;
  model: string;
  upstream_model: string | null;
  streamed: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  cached_prompt_tokens: number;
  cost: string;
  currency: string;
  status: string;
  latency_ms: number | null;
  source: "api_key" | "token";
}

export const REQUEST_STATUSES = [
  "completed",
  "blocked",
  "upstream_error",
  "client_disconnected",
  "in_progress",
] as const;

export interface RequestLogQuery {
  period: string;
  userId?: string;
  groupId?: string;
  model?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

/**
 * The filter half of the query string, shared by the listing and the CSV so
 * that an export is always the set on screen. `userId` is only ever sent by
 * the administrator screen; the personal endpoint ignores it server-side.
 */
export function requestFilterString(query: RequestLogQuery): string {
  const params = new URLSearchParams();
  if (query.period) params.set("period", query.period);
  if (query.userId) params.set("user_id", query.userId);
  if (query.groupId) params.set("group_id", query.groupId);
  if (query.model) params.set("model", query.model);
  if (query.status) params.set("status", query.status);
  return params.toString();
}

export type RequestLogScope = "admin" | "me";

export function requestLogBase(scope: RequestLogScope): string {
  return scope === "admin" ? "/api/admin/requests" : "/api/me/requests";
}

export function requestLogCsvPath(scope: RequestLogScope, query: RequestLogQuery): string {
  return `${requestLogBase(scope)}.csv?${requestFilterString(query)}`;
}

export function useRequestLog(scope: RequestLogScope, query: RequestLogQuery) {
  const filters = requestFilterString(query);
  const paging = pageParams({ limit: query.limit ?? 50, offset: query.offset ?? 0 });
  return useQuery({
    queryKey: [scope === "admin" ? "admin" : "me", "requests", filters, paging],
    queryFn: () => request<Page<RequestLogRow>>(`${requestLogBase(scope)}?${filters}&${paging}`),
    placeholderData: keepPreviousData,
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status < 500) && failureCount < 2,
  });
}
