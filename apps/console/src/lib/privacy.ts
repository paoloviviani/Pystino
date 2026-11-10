import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, request } from "./api";
import type { Page } from "./paging";
import type {
  PrivacyAction,
  PrivacyModelType,
  PrivacyMode,
  PrivacyKind,
  PrivacyOptions,
  PrivacyPipeline,
  PrivacyPipelineCheck,
  PrivacyPipelineInput,
  PrivacyPipelineUpdate,
  PolicyTryInput,
  PolicyTryResult,
  PrivacyClassifierInput,
  PrivacyClassifierList,
  PrivacyClassifierUpdate,
  PrivacyEngines,
  PrivacyRuleBody,
  PrivacyRuleView,
  PrivacySettings,
  PrivacyTestInput,
  PrivacyTestResult,
  RedactionPolicyRecord,
  PolicyDefinition,
  RedactionPolicy,
  UserPolicy,
  UserPolicyInput,
} from "./types";
import { m } from "../paraglide/messages.js";
import { MODES } from "./entities";

const MODES_BY_VALUE: Record<string, (typeof MODES)[number]> = Object.fromEntries(
  MODES.map((mode) => [mode.value, mode]),
);

/**
 * Privacy pipelines (ADR 0113, 0114): the two objects, the pipelines that say when and
 * how (rules "When policies + conditions -> mode"), the policies that say what to look
 * for, and the vocabulary both editors share.
 *
 * An administrator's are under `/api/admin/privacy`, a user's own under
 * `/api/me/privacy`: the same shapes, with the user's who fixed to themselves and
 * their actions limited to ones that tighten (the gateway enforces that; the
 * editors only avoid offering what would be refused).
 */

export type Area = "admin" | "me";

const BASE: Record<Area, string> = { admin: "/api/admin/privacy", me: "/api/me/privacy" };

export const privacyKeys = {
  pipelines: (area: Area) => [area, "privacy", "pipelines"] as const,
  policies: (area: Area) => [area, "privacy", "policies"] as const,
  engines: ["admin", "privacy", "engines"] as const,
  classifiers: ["admin", "privacy", "classifiers"] as const,
  options: (area: Area) => [area, "privacy", "options"] as const,
  settings: ["admin", "privacy", "settings"] as const,
  inForce: ["me", "privacy", "in-force"] as const,
};

function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

export function usePipelines(area: Area) {
  return useQuery({
    queryKey: privacyKeys.pipelines(area),
    queryFn: () => request<Page<PrivacyPipeline>>(`${BASE[area]}/pipelines?limit=200`),
    retry: retryUnlessRejected,
  });
}

export function useInForce() {
  return useQuery({
    queryKey: privacyKeys.inForce,
    queryFn: () => request<Page<PrivacyPipeline>>(`${BASE.me}/in-force?limit=200`),
    retry: retryUnlessRejected,
  });
}

export function usePrivacyOptions(area: Area) {
  return useQuery({
    queryKey: privacyKeys.options(area),
    queryFn: () => request<PrivacyOptions>(`${BASE[area]}/options`),
    retry: retryUnlessRejected,
  });
}

/** The NER engines the redaction service reports and the pattern formats it has. */
export function usePrivacyEngines() {
  return useQuery({
    queryKey: privacyKeys.engines,
    queryFn: () => request<PrivacyEngines>(`${BASE.admin}/engines`),
    retry: retryUnlessRejected,
  });
}

export function useClassifiers() {
  return useQuery({
    queryKey: privacyKeys.classifiers,
    queryFn: () => request<PrivacyClassifierList>(`${BASE.admin}/classifiers`),
    retry: retryUnlessRejected,
  });
}

export function usePolicies() {
  return useQuery({
    queryKey: privacyKeys.policies("admin"),
    queryFn: () => request<Page<RedactionPolicyRecord>>(`${BASE.admin}/policies?limit=200`),
    retry: retryUnlessRejected,
  });
}

export function useMyPolicies() {
  return useQuery({
    queryKey: privacyKeys.policies("me"),
    queryFn: () => request<Page<UserPolicy>>(`${BASE.me}/policies?limit=200`),
    retry: retryUnlessRejected,
  });
}

export function usePrivacySettings() {
  return useQuery({
    queryKey: privacyKeys.settings,
    queryFn: () => request<PrivacySettings>(`${BASE.admin}/settings`),
    retry: retryUnlessRejected,
  });
}

/** A pipeline changes what the policies and classifiers show as "in use". */
function useRefresh(area: Area) {
  const client = useQueryClient();
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: privacyKeys.pipelines(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.policies(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.options(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.classifiers }),
      client.invalidateQueries({ queryKey: privacyKeys.inForce }),
    ]);
}

export function useSavePipeline(area: Area) {
  const refresh = useRefresh(area);
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id?: string;
      body: PrivacyPipelineInput | PrivacyPipelineUpdate;
    }) =>
      id
        ? request<PrivacyPipeline>(`${BASE[area]}/pipelines/${id}`, { method: "PATCH", body })
        : request<PrivacyPipeline>(`${BASE[area]}/pipelines`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useDeletePipeline(area: Area) {
  const refresh = useRefresh(area);
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE[area]}/pipelines/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

export function useReorderPipelines() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: (body: { kind: PrivacyKind; order: string[] }) =>
      request<Page<PrivacyPipeline>>(`${BASE.admin}/pipelines/reorder`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

/** The warnings a draft would get, asked as it is written. Never blocks saving. */
export function useCheckPipeline(area: Area) {
  return useMutation({
    mutationFn: (body: PrivacyPipelineInput) =>
      request<PrivacyPipelineCheck>(`${BASE[area]}/pipelines/check`, { method: "POST", body }),
  });
}

export function useSavePolicy() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id?: string;
      body: {
        name?: string;
        policy?: RedactionPolicy;
        is_active?: boolean;
        shared_with_users?: boolean;
        reason?: string;
      };
    }) =>
      id
        ? request<RedactionPolicyRecord>(`${BASE.admin}/policies/${id}`, { method: "PATCH", body })
        : request<RedactionPolicyRecord>(`${BASE.admin}/policies`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useDeletePolicy() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE.admin}/policies/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

export function useSaveMyPolicy() {
  const refresh = useRefresh("me");
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: UserPolicyInput }) =>
      id
        ? request<UserPolicy>(`${BASE.me}/policies/${id}`, { method: "PUT", body })
        : request<UserPolicy>(`${BASE.me}/policies`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useDeleteMyPolicy() {
  const refresh = useRefresh("me");
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE.me}/policies/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

export function useSaveClassifier() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id?: string;
      body: PrivacyClassifierInput | PrivacyClassifierUpdate;
    }) =>
      id
        ? request<PrivacyClassifierList["items"][number]>(`${BASE.admin}/classifiers/${id}`, {
            method: "PATCH",
            body,
          })
        : request<PrivacyClassifierList["items"][number]>(`${BASE.admin}/classifiers`, {
            method: "POST",
            body,
          }),
    onSuccess: refresh,
  });
}

export function useDeleteClassifier() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: (id: string) =>
      request<void>(`${BASE.admin}/classifiers/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

/** Try a policy as it is being edited, unsaved, on a sample: real detection, no side effects. */
export function useTryPolicy() {
  return useMutation({
    mutationFn: (body: PolicyTryInput) =>
      request<PolicyTryResult>(`${BASE.admin}/policies/try`, { method: "POST", body }),
  });
}

export function useSavePrivacySettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { user_entity_types: string[] | null }) =>
      request<PrivacySettings>(`${BASE.admin}/settings`, { method: "PUT", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: privacyKeys.settings }),
  });
}

/** The test box: a mutation, because it runs the real path and its body is real text. */
export function usePrivacyTest(area: Area) {
  return useMutation({
    mutationFn: (body: PrivacyTestInput) =>
      request<PrivacyTestResult>(`${BASE[area]}/test`, { method: "POST", body }),
  });
}

// -- the vocabulary ---------------------------------------------------------------

export const KINDS: PrivacyKind[] = ["extract", "content"];

export function kindLabel(kind: string): string {
  switch (kind) {
    case "extract":
    case "read":
      return m["privacy.kind.extract"]();
    case "content":
      return m["privacy.kind.content"]();
    // Log entries keep the kind they were written with: embeddings and chat.
    case "embed":
      return m["privacy.kind.embed"]();
    case "chat":
      return m["privacy.kind.chat"]();
    default:
      return kind;
  }
}

export function kindHint(kind: PrivacyKind): string {
  return kind === "extract" ? m["privacy.kind.extract_hint"]() : m["privacy.kind.content_hint"]();
}

export const MODEL_TYPES: PrivacyModelType[] = ["chat", "embedding", "rerank"];

export function modelTypeLabel(value: string): string {
  switch (value) {
    case "chat":
      return m["privacy.edit.model_type_chat"]();
    case "embedding":
      return m["privacy.edit.model_type_embedding"]();
    case "rerank":
      return m["privacy.edit.model_type_rerank"]();
    default:
      return m["privacy.edit.model_type_any"]();
  }
}

/** What an extract rule does with the file, lowest to highest. */
export const EXTRACT_LEVELS = ["default", "extract_with", "block"];

export function levelLabel(level: string): string {
  switch (level) {
    case "default":
      return m["privacy.level.default"]();
    case "extract_with":
      return m["privacy.level.extract_with"]();
    case "block":
      return m["privacy.level.block"]();
    case "pass":
      return m["privacy.level.pass"]();
    case "pending":
      return m["privacy.level.pending"]();
    default:
      return level;
  }
}

export function levelHint(level: string): string {
  switch (level) {
    case "default":
      return m["privacy.level.default_hint"]();
    case "extract_with":
      return m["privacy.level.read_with_hint"]();
    default:
      return m["privacy.level.block_hint"]();
  }
}

/** The extractor an "extract with" rule that names none means: the built-in default-engine model. */
export const DEFAULT_READER = "markitdown";

/**
 * What a rule may test. `classified` only to an administrator (it sends the text to
 * a model's provider). An extract rule decides from the file, never its text.
 */
export function conditionKeys(kind: PrivacyKind, own: boolean): string[] {
  if (kind === "extract") return ["filetype", "scan", "filename"];
  return own ? ["finds"] : ["finds", "classified"];
}
/** Yes/no conditions: they carry no values, only "is" and "is not". */
export const BOOL_KEYS = ["scan"];
export const FILETYPES = ["pdf", "word", "excel", "powerpoint", "image", "text"];

export function conditionLabel(key: string): string {
  switch (key) {
    case "filetype":
      return m["privacy.cond.filetype"]();
    case "scan":
      return m["privacy.cond.scan"]();
    case "filename":
      return m["privacy.cond.filename"]();
    case "finds":
      return m["privacy.cond.finds_rule"]();
    case "classified":
      return m["privacy.cond.classified"]();
    default:
      return key;
  }
}

export function conditionHint(key: string): string {
  switch (key) {
    case "filetype":
      return m["privacy.cond.filetype_hint"]();
    case "scan":
      return m["privacy.cond.scan_hint"]();
    case "filename":
      return m["privacy.cond.filename_hint"]();
    case "finds":
      return m["privacy.cond.finds_hint"]();
    default:
      return m["privacy.cond.classified_hint"]();
  }
}

export function filetypeLabel(value: string): string {
  switch (value) {
    case "pdf":
      return m["privacy.filetype.pdf"]();
    case "word":
      return m["privacy.filetype.word"]();
    case "excel":
      return m["privacy.filetype.excel"]();
    case "powerpoint":
      return m["privacy.filetype.powerpoint"]();
    case "image":
      return m["privacy.filetype.image"]();
    case "text":
      return m["privacy.filetype.text"]();
    default:
      return value;
  }
}

export function valueLabel(key: string, value: string): string {
  if (key === "filetype") return filetypeLabel(value);
  return value;
}

export type Labels = Readonly<Record<string, string>>;

/** A condition as the editors hold it: values and negate always present. */
export interface Cond {
  key: string;
  values: string[];
  negate: boolean;
  /** `finds`: how many items make it true (at least this many). */
  count?: number;
}

type Phrasable = Pick<Cond, "key" | "values" | "negate"> & { count?: number };

/** One condition as words: "it is a PDF", "it finds at least 2 items". */
export function conditionPhrase(c: Phrasable, labels: Labels = {}): string {
  const values = c.values.map((v) => valueLabel(c.key, v)).join(" / ");
  const named = c.values.map((v) => labels[v] ?? "?").join(" / ");
  switch (c.key) {
    case "finds": {
      const one = (c.count ?? 1) === 1;
      if (one) {
        return c.negate ? m["privacy.phrase.finds_fewer_one"]() : m["privacy.phrase.finds_one"]();
      }
      return c.negate
        ? m["privacy.phrase.finds_fewer"]({ count: String(c.count ?? 1) })
        : m["privacy.phrase.finds"]({ count: String(c.count ?? 1) });
    }
    case "classified":
      return c.negate
        ? m["privacy.phrase.not_classified"]({ classifier: named })
        : m["privacy.phrase.classified"]({ classifier: named });
    case "scan":
      return c.negate ? m["privacy.phrase.not_scan"]() : m["privacy.phrase.scan"]();
    case "filename":
      return c.negate
        ? m["privacy.phrase.name_not"]({ values })
        : m["privacy.phrase.name_is"]({ values });
    case "filetype":
      return c.negate
        ? m["privacy.phrase.type_not"]({ values })
        : m["privacy.phrase.type_is"]({ values });
    default:
      return values;
  }
}

/** The six modes, weakest to strongest, as the rule's "then". */
export const RULE_MODES: PrivacyMode[] = [
  "off",
  "detect_only",
  "anonymise_restore",
  "anonymise",
  "redact",
  "block",
];

export function ruleModeLabel(mode: string): string {
  return MODES_BY_VALUE[mode]?.label() ?? mode;
}

interface SentenceRule {
  policies?: readonly string[];
  match?: string;
  conditions: { key: string; values?: string[]; negate?: boolean; count?: number }[];
  mode?: string;
  overrides?: Readonly<Record<string, string>>;
  action?: { level: string; reader?: string | null } | null;
}

/** A rule as one sentence: "When [policies] + [conditions] → [mode]". */
export function ruleSentence(kind: PrivacyKind, rule: SentenceRule, labels: Labels): string {
  const join =
    rule.match === "any"
      ? m["privacy.rule.conditions_join_any"]()
      : m["privacy.rule.conditions_join_all"]();
  const conds = rule.conditions
    .map((c) =>
      conditionPhrase(
        { key: c.key, values: c.values ?? [], negate: c.negate ?? false, count: c.count },
        labels,
      ),
    )
    .join(join);
  const names = (rule.policies ?? []).map((p) => labels[p] ?? "?").join(m["privacy.rule.plus"]());
  const overrides = Object.keys(rule.overrides ?? {}).length;
  const mode =
    ruleModeLabel(rule.mode ?? "off") +
    (overrides
      ? ` (${
          overrides === 1
            ? m["privacy.rule.overrides_count_one"]()
            : m["privacy.rule.overrides_count"]({ count: String(overrides) })
        })`
      : "");
  if (kind === "extract") {
    const a = rule.action ?? { level: "default" };
    const action =
      a.level === "block"
        ? m["privacy.action.block"]()
        : a.level === "extract_with"
          ? m["privacy.action.extract_with"]({ reader: a.reader || DEFAULT_READER })
          : m["privacy.action.default"]();
    const head = m["privacy.rule.sentence_extract"]({
      conditions: conds || m["privacy.rule.all_requests"](),
      action,
    });
    return names && rule.mode && rule.mode !== "off"
      ? m["privacy.rule.sentence_hide"]({ action: head, mode: mode.toLowerCase(), policies: names })
      : head;
  }
  return m["privacy.rule.sentence"]({
    policies: names || m["privacy.rule.no_policies"](),
    conditions: conds ? ` + ${conds}` : "",
    mode: mode.toLowerCase(),
  });
}

/** The scope of a pipeline as two short phrases: who, and which target. */
export function scopeWords(p: PrivacyPipeline, own: boolean): [string, string] {
  const names = (ids: readonly string[]) => ids.map((id) => p.labels[id] ?? "?").join(", ");
  const who = own
    ? m["privacy.scope.me"]()
    : p.who.mode === "all"
      ? m["privacy.scope.all_users"]()
      : p.who.mode === "users"
        ? m["privacy.scope.users"]({ names: names(p.who.ids) })
        : p.who.mode === "groups"
          ? m["privacy.scope.groups"]({ names: names(p.who.ids) })
          : m["privacy.scope.keys"]({ names: names(p.who.ids) });
  const targets = names([...p.target.providers, ...p.target.models]);
  const base =
    p.target.mode === "all"
      ? m["privacy.scope.all_targets"]()
      : p.target.mode === "only"
        ? m["privacy.scope.only"]({ names: targets })
        : m["privacy.scope.except"]({ names: targets });
  const types = p.target.model_types ?? [];
  const target = types.length
    ? `${base} · ${types.map((t) => modelTypeLabel(t)).join(" + ")}`
    : base;
  return [who, target];
}

/** A rule's warnings, flattened into one badge: there is no shadowing any more. */
export function issueBadges(rule: PrivacyRuleView): { check: boolean } {
  return { check: rule.issues.length > 0 };
}

/** What the save sends for one rule. */
export function ruleBody(rule: PrivacyRuleView): PrivacyRuleBody {
  return {
    id: rule.id,
    name: rule.name,
    enabled: rule.enabled,
    note: rule.note ?? null,
    policies: [...rule.policies],
    match: rule.match,
    mode: rule.mode,
    overrides: { ...rule.overrides },
    conditions: rule.conditions.map((c) => ({
      key: c.key,
      values: [...(c.values ?? [])],
      negate: c.negate ?? false,
      ...(c.key === "finds" ? { count: c.count ?? 1 } : {}),
    })),
    ...(rule.action ? { action: actionBody(rule.action) } : {}),
  };
}

export function actionBody(a: { level: string; reader?: string | null }): PrivacyAction {
  return { level: a.level, ...(a.reader ? { reader: a.reader } : {}) };
}

// -- policies: what to look for, no mode -------------------------------------------

/**
 * A stored policy as the policy editor holds it. The editor's controls speak the
 * request path's own shape, which has a mode on each item; a stored policy has none
 * (ADR 0114), so one is written on here and never shown (the editor runs detection-only).
 */
export function withModes(def: PolicyDefinition): RedactionPolicy {
  const stamp = <T extends object>(item: T) => ({ ...item, mode: "redact" as const });
  return {
    default_mode: "off",
    engine: (def.engine ?? null) as RedactionPolicy["engine"],
    allow_list: [...(def.allow_list ?? [])],
    formats: Object.fromEntries(Object.entries(def.formats ?? {}).map(([k, v]) => [k, stamp(v)])),
    entities: Object.fromEntries(Object.entries(def.entities ?? {}).map(([k, v]) => [k, stamp(v)])),
    custom_entities: (def.custom_entities ?? []).map(stamp),
    patterns: (def.patterns ?? []).map(stamp),
    keywords: (def.keywords ?? []).map(stamp),
  } as RedactionPolicy;
}

/** The inverse of `withModes`: what a save sends. */
export function withoutModes(policy: RedactionPolicy): PolicyDefinition {
  const strip = <T extends { mode?: unknown }>(item: T) => {
    const { mode: _mode, ...rest } = item;
    return rest;
  };
  return {
    engine: policy.engine ?? null,
    allow_list: [...policy.allow_list],
    formats: Object.fromEntries(Object.entries(policy.formats).map(([k, v]) => [k, strip(v)])),
    entities: Object.fromEntries(Object.entries(policy.entities).map(([k, v]) => [k, strip(v)])),
    custom_entities: policy.custom_entities.map(strip),
    patterns: policy.patterns.map(strip),
    keywords: policy.keywords.map(strip),
  } as PolicyDefinition;
}

/** A policy in one line: how much it looks for. */
export function summariseDefinition(policy: PolicyDefinition): string {
  const parts: string[] = [];
  const entities = Object.keys(policy.entities ?? {}).length + (policy.custom_entities ?? []).length;
  if (entities > 0) parts.push(m["policy_summary.types"]({ count: entities }));
  const formats = Object.keys(policy.formats ?? {}).length;
  if (formats > 0) parts.push(m["policy_summary.formats"]({ count: formats }));
  if ((policy.patterns ?? []).length > 0) {
    parts.push(m["policy_summary.patterns"]({ count: (policy.patterns ?? []).length }));
  }
  if ((policy.keywords ?? []).length > 0) {
    parts.push(m["policy_summary.keywords"]({ count: (policy.keywords ?? []).length }));
  }
  if (policy.engine) parts.push(m["policy_summary.engine"]({ engine: policy.engine }));
  if ((policy.allow_list ?? []).length > 0) {
    parts.push(m["policy_summary.allow_list"]({ count: (policy.allow_list ?? []).length }));
  }
  return parts.join(" · ");
}
