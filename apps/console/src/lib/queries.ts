import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import { MAX_LIMIT, type Page, pageParams } from "./paging";
import type { ApiKey, MintedApiKey, Me, UsageReport } from "./types";

/**
 * Query keys, in one place.
 *
 * A key typed inline at the call site is a cache entry nothing else can
 * invalidate, and the resulting bug — a mutation that succeeds while the screen
 * keeps showing the old number — looks like a backend problem.
 */
export const keys = {
  me: ["me"] as const,
  myKeys: ["me", "keys"] as const,
  myReport: (period: string, groupBy: string) => ["me", "report", period, groupBy] as const,
};

/**
 * Retry policy: never retry a request the server rejected.
 *
 * Any 4xx means *this request* was wrong — an expired session, a period the API
 * cannot parse, a scope the caller may not read. Sending it again produces the
 * same answer, so the default three retries only delay the message the reader
 * needs by several seconds. Retries are for 5xx and for the network dropping,
 * which are the failures that might not happen twice.
 */
function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => request<Me>("/api/me"),
    retry: retryUnlessRejected,
    // Identity and group membership change on human timescales, and every
    // screen reads them. Refetching on each mount would be a request per
    // navigation for a value that has not moved.
    staleTime: 5 * 60_000,
  });
}

export function useMyReport(period: string, groupBy: string) {
  const query = period ? `period=${encodeURIComponent(period)}&` : "";
  return useQuery({
    queryKey: keys.myReport(period, groupBy),
    queryFn: () =>
      request<UsageReport>(`/api/me/reports/usage?${query}group_by=${encodeURIComponent(groupBy)}`),
    retry: retryUnlessRejected,
  });
}

export function useMyKeys() {
  // One person's keys, so the whole set fits in a page and the overview does
  // not need a pager. It still reads the envelope, because the endpoint
  // returns one.
  const search = pageParams({ limit: MAX_LIMIT });
  return useQuery({
    queryKey: [...keys.myKeys, search],
    queryFn: () => request<Page<ApiKey>>(`/api/me/keys?${search}`),
    retry: retryUnlessRejected,
  });
}

export interface MintKeyInput {
  name: string;
  /** Omitted means "resolve the user's default group at request time". */
  billing_group_id?: string | null;
  expires_in_days?: number | null;
}

/**
 * Mint a key.
 *
 * The response carries the only copy of the secret that will ever exist, so it
 * is returned to the caller rather than swallowed — and deliberately **not**
 * written into the query cache. A cached secret would survive in memory for as
 * long as the tab is open and could be re-rendered by any later read of the
 * key list, which is exactly the "shown once" promise broken quietly.
 */
export function useMintKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: MintKeyInput) =>
      request<MintedApiKey>("/api/me/keys", { method: "POST", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.myKeys }),
  });
}

/**
 * Revoke one. The API revokes rather than deletes — the usage ledger references
 * keys, and removing one would turn historical spend into an unattributable row.
 */
export function useRevokeKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<ApiKey>(`/api/me/keys/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.myKeys }),
  });
}
