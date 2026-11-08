import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, request } from "./api";
import type { Page } from "./paging";
import type {
  PrivacyAction,
  PrivacyKind,
  PrivacyOptions,
  PrivacyPipeline,
  PrivacyPipelineCheck,
  PrivacyPipelineInput,
  PrivacyPipelineUpdate,
  PrivacyProfile,
  PrivacyProfileInput,
  PrivacyProfileUpdate,
  PrivacyRuleBody,
  PrivacyRuleView,
  PrivacySettings,
  PrivacyTestInput,
  PrivacyTestResult,
  RedactionPolicyRecord,
  RedactionPolicy,
  UserPolicy,
  UserPolicyInput,
} from "./types";
import { m } from "../paraglide/messages.js";

/**
 * Privacy pipelines (ADR 0113): the two objects, the pipelines that say when and
 * how, the policies that say what to hide, and the vocabulary both editors share.
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
  profiles: ["admin", "privacy", "profiles"] as const,
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

export function usePrivacyProfiles() {
  return useQuery({
    queryKey: privacyKeys.profiles,
    queryFn: () => request<Page<PrivacyProfile>>(`${BASE.admin}/profiles?limit=200`),
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

/** A pipeline changes what the profiles and policies show as "in use". */
function useRefresh(area: Area) {
  const client = useQueryClient();
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: privacyKeys.pipelines(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.policies(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.options(area) }),
      client.invalidateQueries({ queryKey: privacyKeys.profiles }),
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
        profile_id?: string | null;
        is_active?: boolean;
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

export function useCreatePrivacyProfile() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: (body: PrivacyProfileInput) =>
      request<PrivacyProfile>(`${BASE.admin}/profiles`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useUpdatePrivacyProfile() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PrivacyProfileUpdate }) =>
      request<PrivacyProfile>(`${BASE.admin}/profiles/${id}`, { method: "PATCH", body }),
    onSuccess: refresh,
  });
}

export function useDeletePrivacyProfile() {
  const refresh = useRefresh("admin");
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE.admin}/profiles/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
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

export const KINDS: PrivacyKind[] = ["read", "embed", "chat"];

export function kindLabel(kind: string): string {
  switch (kind) {
    case "read":
      return m["privacy.kind.read"]();
    case "embed":
      return m["privacy.kind.embed"]();
    case "chat":
      return m["privacy.kind.chat"]();
    default:
      return kind;
  }
}

export function kindHint(kind: PrivacyKind): string {
  return kind === "read"
    ? m["privacy.kind.read_hint"]()
    : kind === "embed"
      ? m["privacy.kind.embed_hint"]()
      : m["privacy.kind.chat_hint"]();
}

/** The ladder, lowest to highest. The highest level across all pipelines wins. */
export const LADDER: Record<PrivacyKind, string[]> = {
  read: ["default", "read_with", "refuse"],
  embed: ["pass", "refuse"],
  chat: ["pass", "refuse"],
};

export function baseLevel(kind: PrivacyKind): string {
  return LADDER[kind][0] ?? "pass";
}

export function levelLabel(level: string): string {
  switch (level) {
    case "default":
      return m["privacy.level.default"]();
    case "read_with":
      return m["privacy.level.read_with"]();
    case "refuse":
      return m["privacy.level.refuse"]();
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
    case "read_with":
      return m["privacy.level.read_with_hint"]();
    case "refuse":
      return m["privacy.level.refuse_hint"]();
    default:
      return m["privacy.level.pass_hint"]();
  }
}

export const BUILTIN = "builtin";
export const ENGINES = ["markitdown", "ocr", "docling"];

export function engineLabel(engine: string): string {
  switch (engine) {
    case "markitdown":
      return m["privacy.engine.markitdown"]();
    case "ocr":
      return m["privacy.engine.ocr"]();
    case "docling":
      return m["privacy.engine.docling"]();
    default:
      return m["privacy.engine.default"]();
  }
}

export const CONDITION_KEYS: Record<PrivacyKind, string[]> = {
  read: ["filetype", "scan", "filename"],
  embed: ["health", "ids", "lang"],
  chat: ["health", "ids", "lang"],
};
/** Yes/no conditions: they carry no values, only "is" and "is not". */
export const BOOL_KEYS = ["scan", "health", "ids"];
/** Conditions decided by reading the request's own text. */
export const CONTENT_KEYS = ["health", "ids", "lang"];
export const FILETYPES = ["pdf", "word", "excel", "powerpoint", "image", "text"];
export const LANGUAGES = ["it", "en", "fr", "de", "es"];
export const PATTERNS = "patterns";

export function conditionLabel(key: string): string {
  switch (key) {
    case "filetype":
      return m["privacy.cond.filetype"]();
    case "scan":
      return m["privacy.cond.scan"]();
    case "filename":
      return m["privacy.cond.filename"]();
    case "health":
      return m["privacy.cond.health"]();
    case "ids":
      return m["privacy.cond.ids"]();
    case "lang":
      return m["privacy.cond.lang"]();
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
    case "health":
      return m["privacy.cond.health_hint"]();
    case "ids":
      return m["privacy.cond.ids_hint"]();
    default:
      return m["privacy.cond.lang_hint"]();
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

export function languageLabel(value: string): string {
  switch (value) {
    case "it":
      return m["privacy.lang.it"]();
    case "en":
      return m["privacy.lang.en"]();
    case "fr":
      return m["privacy.lang.fr"]();
    case "de":
      return m["privacy.lang.de"]();
    case "es":
      return m["privacy.lang.es"]();
    default:
      return value;
  }
}

export function valueLabel(key: string, value: string): string {
  if (key === "filetype") return filetypeLabel(value);
  if (key === "lang") return languageLabel(value);
  return value;
}

export type Labels = Readonly<Record<string, string>>;

/** A condition as the editors hold it: values and negate always present. */
export interface Cond {
  key: string;
  values: string[];
  negate: boolean;
  decide?: string | null;
}

type Phrasable = Pick<Cond, "key" | "values" | "negate">;

/** One condition as words: "contains health data", "is a PDF or Word file". */
export function conditionPhrase(c: Phrasable): string {
  const values = c.values.map((v) => valueLabel(c.key, v)).join(" / ");
  switch (c.key) {
    case "health":
      return c.negate ? m["privacy.phrase.no_health"]() : m["privacy.phrase.health"]();
    case "ids":
      return c.negate ? m["privacy.phrase.no_ids"]() : m["privacy.phrase.ids"]();
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
    case "lang":
      return c.negate
        ? m["privacy.phrase.lang_not"]({ values })
        : m["privacy.phrase.lang_is"]({ values });
    default:
      return values;
  }
}

interface Actionable {
  level: string;
  reader?: string | null;
  engine?: string | null;
  redact_with?: string | null;
}

/** What an action does, in words. `labels` names policies by id. */
export function actionPhrase(kind: PrivacyKind, a: Actionable, labels: Labels): string {
  const parts: string[] = [];
  if (a.level === "refuse") parts.push(m["privacy.action.refuse"]());
  else if (a.level === "read_with") {
    const reader =
      !a.reader || a.reader === BUILTIN ? m["privacy.reader.builtin"]() : a.reader;
    parts.push(m["privacy.action.read_with"]({ reader }));
    if (a.engine) parts.push(m["privacy.action.engine"]({ engine: engineLabel(a.engine) }));
  } else if (a.level === baseLevel(kind) && !a.redact_with) {
    parts.push(
      kind === "read" ? m["privacy.action.default"]() : m["privacy.action.pass"](),
    );
  }
  if (a.redact_with) {
    parts.push(m["privacy.action.redact_with"]({ policy: labels[a.redact_with] ?? "?" }));
  }
  return parts.join(" · ");
}

interface SentenceRule {
  conditions: { key: string; values?: string[]; negate?: boolean }[];
  action: Actionable;
  observe_only?: boolean;
}

export function ruleSentence(kind: PrivacyKind, rule: SentenceRule, labels: Labels): string {
  const when = rule.conditions.length
    ? rule.conditions
        .map((c) => conditionPhrase({ key: c.key, values: c.values ?? [], negate: c.negate ?? false }))
        .join(m["privacy.phrase.and"]())
    : m["privacy.phrase.any"]();
  const text = m["privacy.phrase.rule"]({ when, action: actionPhrase(kind, rule.action, labels) });
  return rule.observe_only ? `${text} ${m["privacy.phrase.observe"]()}` : text;
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
  const target =
    p.target.mode === "all"
      ? m["privacy.scope.all_targets"]()
      : p.target.mode === "only"
        ? m["privacy.scope.only"]({ names: targets })
        : m["privacy.scope.except"]({ names: targets });
  return [who, target];
}

/** A pipeline's rules warnings, flattened: never-fires and partly-shadowed get their own badges. */
export function issueBadges(rule: PrivacyRuleView): {
  never: boolean;
  partly: boolean;
  check: boolean;
} {
  return {
    never: rule.issues.some((i) => i.code === "never_fires"),
    partly: rule.issues.some((i) => i.code === "partly_shadowed"),
    check: rule.issues.some((i) => i.code !== "never_fires" && i.code !== "partly_shadowed"),
  };
}

/** What the save sends for one rule. */
export function ruleBody(rule: PrivacyRuleView): PrivacyRuleBody {
  return {
    id: rule.id,
    name: rule.name,
    enabled: rule.enabled,
    observe_only: rule.observe_only,
    conditions: rule.conditions.map((c) => ({
      key: c.key,
      values: [...(c.values ?? [])],
      negate: c.negate ?? false,
      ...(c.decide ? { decide: c.decide } : {}),
    })),
    action: actionBody(rule.action),
  };
}

export function actionBody(a: Actionable): PrivacyAction {
  return {
    level: a.level,
    ...(a.reader ? { reader: a.reader } : {}),
    ...(a.engine ? { engine: a.engine } : {}),
    ...(a.redact_with ? { redact_with: a.redact_with } : {}),
  };
}
