import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import type {
  AdminGroup,
  AdminModel,
  AdminProvider,
  AdminUser,
  CatalogueDiscovery,
  LimitRule,
  ModelImportResponse,
  Price,
  ProviderTestResult,
  QuotaReset,
  UsageReport,
} from "./types";

/**
 * Administrative queries and mutations.
 *
 * Every mutation invalidates the keys its change could have affected, listed
 * explicitly rather than by clearing the cache. A mutation that succeeds while
 * the screen still shows the old number reads as a backend bug, and it is the
 * commonest way for an admin UI to lie.
 */

export const adminKeys = {
  providers: ["admin", "providers"] as const,
  models: ["admin", "models"] as const,
  prices: (modelId: string) => ["admin", "models", modelId, "prices"] as const,
  discovery: ["admin", "models", "discover"] as const,
  groups: ["admin", "groups"] as const,
  limits: ["admin", "limits"] as const,
  resets: (ruleId: string) => ["admin", "limits", ruleId, "resets"] as const,
  users: ["admin", "users"] as const,
  report: (query: string) => ["admin", "report", query] as const,
};

/** No 4xx is retried: the request will be just as wrong the second time. */
function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

// -- providers ---------------------------------------------------------------

export function useProviders() {
  return useQuery({
    queryKey: adminKeys.providers,
    queryFn: () => request<AdminProvider[]>("/api/admin/providers"),
    retry: retryUnlessRejected,
  });
}

export interface ProviderInput {
  name?: string;
  description?: string | null;
  base_url?: string;
  /**
   * Three-way, matching the API: omitted keeps the stored key, a value replaces
   * it, an empty string clears it. A plain optional string cannot express
   * "remove the credential", so the form must send `undefined` and not `""`
   * when the field was left alone.
   */
  api_key?: string;
  extra_headers?: Record<string, string>;
  is_active?: boolean;
  forward_stream_options?: boolean;
}

export function useCreateProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: ProviderInput) =>
      request<AdminProvider>("/api/admin/providers", { method: "POST", body: input }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.providers }),
  });
}

export function useUpdateProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: ProviderInput & { id: string }) =>
      request<AdminProvider>(`/api/admin/providers/${id}`, { method: "PATCH", body }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: adminKeys.providers });
      // A deactivated provider takes its models out of service, and the
      // catalogue shows that per model.
      client.invalidateQueries({ queryKey: adminKeys.models });
    },
  });
}

export function useDeleteProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>(`/api/admin/providers/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.providers }),
  });
}

/**
 * Test a provider's configuration.
 *
 * A failing test is a 200 with `ok: false`, so this mutation resolves rather
 * than rejects for a provider-side problem — the detail is the useful part and
 * an error state would hide it behind a generic message.
 */
export function useTestProvider() {
  return useMutation({
    mutationFn: (id: string) =>
      request<ProviderTestResult>(`/api/admin/providers/${id}/test`, { method: "POST" }),
  });
}

// -- catalogue ---------------------------------------------------------------

export function useModels() {
  return useQuery({
    queryKey: adminKeys.models,
    queryFn: () => request<AdminModel[]>("/api/admin/models"),
    retry: retryUnlessRejected,
  });
}

export function usePrices(modelId: string | null) {
  return useQuery({
    queryKey: adminKeys.prices(modelId ?? ""),
    queryFn: () => request<Price[]>(`/api/admin/models/${modelId}/prices`),
    enabled: modelId !== null,
    retry: retryUnlessRejected,
  });
}

export function useGroups() {
  return useQuery({
    queryKey: adminKeys.groups,
    queryFn: () => request<AdminGroup[]>("/api/admin/groups"),
    retry: retryUnlessRejected,
  });
}

/**
 * Provider catalogue discovery.
 *
 * Not fetched on mount: it calls out to the provider over the network and can be
 * slow or down, and a page that hangs on load because a third party is having a
 * bad day is worse than one with a button on it.
 */
export function useDiscovery(providerId: string | null) {
  return useQuery({
    queryKey: [...adminKeys.discovery, providerId],
    queryFn: () =>
      request<CatalogueDiscovery>(
        `/api/admin/models/discover?provider_id=${encodeURIComponent(providerId ?? "")}`,
      ),
    enabled: providerId !== null,
    retry: false,
    staleTime: 60_000,
  });
}

export interface CreateModelInput {
  name: string;
  upstream_model: string;
  /** Required: a model with no provider cannot be routed (ADR 0027). */
  provider_id: string;
  kind?: "chat" | "embedding";
  context_window?: number | null;
}

export function useCreateModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateModelInput) =>
      request<AdminModel>("/api/admin/models", { method: "POST", body: input }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.models }),
  });
}

export function useUpdateModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; is_active?: boolean; provider_id?: string }) =>
      request<AdminModel>(`/api/admin/models/${id}`, { method: "PATCH", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.models }),
  });
}

export function useImportModels() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ providerId, upstreamModels }: { providerId: string; upstreamModels: string[] }) =>
      request<ModelImportResponse>(
        `/api/admin/models/import?provider_id=${encodeURIComponent(providerId)}`,
        {
          method: "POST",
          body: { models: upstreamModels.map((upstream_model) => ({ upstream_model })) },
        },
      ),
    onSuccess: () => {
      // Both: the catalogue gained rows, and discovery's "available" list lost them.
      client.invalidateQueries({ queryKey: adminKeys.models });
      client.invalidateQueries({ queryKey: adminKeys.discovery });
    },
  });
}

// -- access ------------------------------------------------------------------

export function useModelAccess() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, modelId, grant }: { groupId: string; modelId: string; grant: boolean }) =>
      request<void>(`/api/admin/groups/${groupId}/models/${modelId}`, {
        method: grant ? "PUT" : "DELETE",
      }),
    onSuccess: () => {
      // Access is denormalised into both listings, so both are now stale.
      client.invalidateQueries({ queryKey: adminKeys.models });
      client.invalidateQueries({ queryKey: adminKeys.groups });
    },
  });
}

/** Personal model grants, unioned with group grants at request time. */
export function useUserModelAccess() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, modelId, grant }: { userId: string; modelId: string; grant: boolean }) =>
      request<void>(`/api/admin/users/${userId}/models/${modelId}`, {
        method: grant ? "PUT" : "DELETE",
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.models }),
  });
}

// -- pricing -----------------------------------------------------------------

export interface CreatePriceInput {
  modelId: string;
  input_per_mtok: string;
  output_per_mtok: string;
  effective_from?: string | null;
}

export function useCreatePrice() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ modelId, ...body }: CreatePriceInput) =>
      request<Price>(`/api/admin/models/${modelId}/prices`, { method: "POST", body }),
    onSuccess: (_price, variables) => {
      client.invalidateQueries({ queryKey: adminKeys.prices(variables.modelId) });
      // The catalogue shows each model's current price, which may have changed.
      client.invalidateQueries({ queryKey: adminKeys.models });
    },
  });
}

// -- quotas ------------------------------------------------------------------

export function useLimits() {
  return useQuery({
    queryKey: adminKeys.limits,
    queryFn: () => request<LimitRule[]>("/api/admin/limits"),
    retry: retryUnlessRejected,
    // Consumption moves with traffic, so a quota page left open should not go
    // stale in a way that hides an exhausted budget.
    refetchInterval: 30_000,
  });
}

export function useResets(ruleId: string | null) {
  return useQuery({
    queryKey: adminKeys.resets(ruleId ?? ""),
    queryFn: () => request<QuotaReset[]>(`/api/admin/limits/${ruleId}/resets`),
    enabled: ruleId !== null,
    retry: retryUnlessRejected,
  });
}

export interface CreateLimitInput {
  name: string;
  scope: string;
  scope_id: string | null;
  metric: string;
  window_seconds: number | null;
  period: string | null;
  limit_value: string;
}

export function useCreateLimit() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateLimitInput) =>
      request<LimitRule>("/api/admin/limits", { method: "POST", body: input }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.limits }),
  });
}

export function useUpdateLimit() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; limit_value?: string; is_active?: boolean }) =>
      request<LimitRule>(`/api/admin/limits/${id}`, { method: "PATCH", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.limits }),
  });
}

export function useDeleteLimit() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>(`/api/admin/limits/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.limits }),
  });
}

export function useResetLimit() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      request<QuotaReset>(`/api/admin/limits/${id}/reset`, { method: "POST", body: { reason } }),
    onSuccess: (_reset, variables) => {
      client.invalidateQueries({ queryKey: adminKeys.limits });
      client.invalidateQueries({ queryKey: adminKeys.resets(variables.id) });
      // Deliberately NOT the reports: a reset moves enforcement and leaves the
      // billing figures exactly where they were (ADR 0025). Invalidating them
      // here would imply otherwise to anyone reading this file.
    },
  });
}

// -- users -------------------------------------------------------------------

export function useUsers() {
  return useQuery({
    queryKey: adminKeys.users,
    queryFn: () => request<AdminUser[]>("/api/admin/users"),
    retry: retryUnlessRejected,
  });
}

export function useUpdateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; is_active?: boolean; is_admin?: boolean }) =>
      request<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.users }),
  });
}

// -- reporting ---------------------------------------------------------------

export interface ReportQuery {
  period: string;
  groupBy: string;
  groupId?: string;
  userId?: string;
  model?: string;
}

export function reportQueryString(query: ReportQuery): string {
  const params = new URLSearchParams();
  if (query.period) params.set("period", query.period);
  params.set("group_by", query.groupBy);
  if (query.groupId) params.set("group_id", query.groupId);
  if (query.userId) params.set("user_id", query.userId);
  if (query.model) params.set("model", query.model);
  return params.toString();
}

export function useAdminReport(query: ReportQuery) {
  const search = reportQueryString(query);
  return useQuery({
    queryKey: adminKeys.report(search),
    queryFn: () => request<UsageReport>(`/api/admin/reports/usage?${search}`),
    retry: retryUnlessRejected,
  });
}
