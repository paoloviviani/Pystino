import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, request } from "./api";
import { type Page, pageParams } from "./paging";
import type {
  MyRedactionInForce,
  RedactionEvent,
  RedactionTestResult,
  UserRedactionRule,
  UserRedactionRuleInput,
} from "./types";

/**
 * A user's own view of redaction: the rules in force, their own rules, the
 * test box and the log of requests where something was hidden.
 *
 * Everything is under `/api/me/redaction`, which the gateway pins to the
 * caller. Nothing here can name another user.
 */

export const redactionKeys = {
  inForce: ["me", "redaction", "in-force"] as const,
  rules: ["me", "redaction", "rules"] as const,
  events: (requestId: string, paging: string) =>
    ["me", "redaction", "events", requestId, paging] as const,
};

const BASE = "/api/me/redaction";

function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

export function useRulesInForce() {
  return useQuery({
    queryKey: redactionKeys.inForce,
    queryFn: () => request<MyRedactionInForce>(`${BASE}/in-force`),
    retry: retryUnlessRejected,
  });
}

export function useMyRedactionRules() {
  return useQuery({
    queryKey: redactionKeys.rules,
    queryFn: () => request<Page<UserRedactionRule>>(`${BASE}/rules?limit=200`),
    retry: retryUnlessRejected,
  });
}

/** Add a rule, or change the one named by `id`. */
export function useSaveRedactionRule() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: UserRedactionRuleInput }) =>
      id
        ? request<UserRedactionRule>(`${BASE}/rules/${id}`, { method: "PUT", body })
        : request<UserRedactionRule>(`${BASE}/rules`, { method: "POST", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: redactionKeys.rules }),
  });
}

export function useDeleteRedactionRule() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE}/rules/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: redactionKeys.rules }),
  });
}

/** The test box. The text is sent and never stored by the gateway. */
export function useTestRedaction() {
  return useMutation({
    mutationFn: (text: string) =>
      request<RedactionTestResult>(`${BASE}/test`, { method: "POST", body: { text } }),
  });
}

export function useRedactionEvents(query: { requestId: string; limit: number; offset: number }) {
  const extra: Record<string, string> = {};
  if (query.requestId.trim()) extra.request_id = query.requestId.trim();
  const paging = pageParams({ limit: query.limit, offset: query.offset }, extra);
  return useQuery({
    queryKey: redactionKeys.events(query.requestId.trim(), paging),
    queryFn: () => request<Page<RedactionEvent>>(`${BASE}/events?${paging}`),
    placeholderData: keepPreviousData,
    retry: retryUnlessRejected,
  });
}
