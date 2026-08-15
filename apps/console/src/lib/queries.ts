import { useQuery } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import type { ApiKey, Me, UsageReport } from "./types";

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
  return useQuery({
    queryKey: keys.myKeys,
    queryFn: () => request<ApiKey[]>("/api/me/keys"),
    retry: retryUnlessRejected,
  });
}
