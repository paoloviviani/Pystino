import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { ApiError, request } from "./api";
import { type Page, pageParams } from "./paging";
import type { RedactionEvent } from "./types";

/**
 * A user's own log of requests where redaction hid something or a pipeline
 * refused: no values in it, and nothing here can name another user.
 *
 * Their own pipelines, policies and test box are in `privacy.ts` (ADR 0113).
 */

export const redactionKeys = {
  events: (requestId: string, paging: string) =>
    ["me", "redaction", "events", requestId, paging] as const,
};

const BASE = "/api/me/redaction";

function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
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
