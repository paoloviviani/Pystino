import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, request } from "./api";
import { MAX_LIMIT, type Page, type PageQuery, pageParams } from "./paging";
import type {
  AdminGroup,
  AdminModel,
  AdminProvider,
  AdminUser,
  CatalogueDiscovery,
  LimitRule,
  ModelImportResponse,
  ModelKind,
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

/**
 * Every listing is a page now, so the window is part of the cache key.
 *
 * Note what is *not* here: the mutations below still invalidate the bare key
 * (`["admin", "users"]`), which prefix-matches every page of it. A mutation
 * that only invalidated the page the operator happens to be on would leave the
 * others stale in the cache, to be shown unchanged the moment they page back.
 */
function pagedKey(base: readonly string[], search: string) {
  return [...base, search] as const;
}

/** No 4xx is retried: the request will be just as wrong the second time. */
function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

// -- providers ---------------------------------------------------------------

/**
 * Keeps the previous page on screen while the next one loads.
 *
 * Without it every keystroke in a search box blanks the table to a spinner,
 * which reads as the results having gone away.
 */
const pagedOptions = {
  placeholderData: keepPreviousData,
  retry: retryUnlessRejected,
} as const;

export function useProviders(query: PageQuery = { limit: MAX_LIMIT }) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.providers, search),
    queryFn: () => request<Page<AdminProvider>>(`/api/admin/providers?${search}`),
    ...pagedOptions,
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
  auth_scheme?: "bearer" | "x_api_key";
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

export function useModels(query: PageQuery = { limit: MAX_LIMIT }) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.models, search),
    queryFn: () => request<Page<AdminModel>>(`/api/admin/models?${search}`),
    ...pagedOptions,
  });
}

export function usePrices(modelId: string | null, query: PageQuery = { limit: MAX_LIMIT }) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.prices(modelId ?? ""), search),
    queryFn: () => request<Page<Price>>(`/api/admin/models/${modelId}/prices?${search}`),
    enabled: modelId !== null,
    ...pagedOptions,
  });
}

export function useGroups(query: PageQuery = { limit: MAX_LIMIT }) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.groups, search),
    queryFn: () => request<Page<AdminGroup>>(`/api/admin/groups?${search}`),
    ...pagedOptions,
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
  kind?: ModelKind;
  context_window?: number | null;
  max_output_tokens?: number | null;
  input_modalities?: string[];
  output_modalities?: string[];
  supported_features?: string[];
}

export function useCreateModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateModelInput) =>
      request<AdminModel>("/api/admin/models", { method: "POST", body: input }),
    onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.models }),
  });
}

/** Every field optional; only what is sent is changed, matching the API. */
export interface ModelUpdateInput {
  is_active?: boolean;
  provider_id?: string;
  kind?: ModelKind;
  display_name?: string | null;
  context_window?: number | null;
  max_output_tokens?: number | null;
  input_modalities?: string[];
  output_modalities?: string[];
  supported_features?: string[];
}

export function useUpdateModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string } & ModelUpdateInput) =>
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
  /** Omitted for a token-priced model; the two are not alternatives. */
  per_image?: string | null;
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

export function useLimits(query: PageQuery = { limit: MAX_LIMIT }) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.limits, search),
    queryFn: () => request<Page<LimitRule>>(`/api/admin/limits?${search}`),
    ...pagedOptions,
    // Consumption moves with traffic, so a quota page left open should not go
    // stale in a way that hides an exhausted budget.
    refetchInterval: 30_000,
  });
}

export function useResets(ruleId: string | null, query: PageQuery = {}) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.resets(ruleId ?? ""), search),
    queryFn: () => request<Page<QuotaReset>>(`/api/admin/limits/${ruleId}/resets?${search}`),
    enabled: ruleId !== null,
    ...pagedOptions,
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

/**
 * Accounts, a page at a time.
 *
 * `enabled` is how the pickers avoid pulling the directory: a dialog that
 * searches for a person passes `false` until something is typed, rather than
 * fetching the first fifty people nobody asked for.
 */
export function useUsers(query: PageQuery = { limit: MAX_LIMIT }, enabled = true) {
  const search = pageParams(query);
  return useQuery({
    queryKey: pagedKey(adminKeys.users, search),
    queryFn: () => request<Page<AdminUser>>(`/api/admin/users?${search}`),
    enabled,
    ...pagedOptions,
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
