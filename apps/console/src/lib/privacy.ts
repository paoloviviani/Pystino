import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, request } from "./api";
import type { Page } from "./paging";
import type {
  PrivacyAction,
  PrivacyCondition,
  PrivacyKind,
  PrivacyProfile,
  PrivacyProfileInput,
  PrivacyProfileUpdate,
  PrivacyRule,
  PrivacyRuleCheck,
  PrivacyRuleInput,
  PrivacyRuleUpdate,
  PrivacyTestInput,
  PrivacyTestResult,
} from "./types";
import { m } from "../paraglide/messages.js";

/**
 * Privacy routing (ADR 0113): the rules an administrator writes per kind of
 * request, the detector profiles they can decide with, and the test box.
 *
 * Everything is under `/api/admin/privacy`. The vocabulary below is the one
 * place that says what a rule can say for each kind, so the list, the builder
 * and the test page agree with the gateway's own table (`privacy/rules.py`).
 */

const BASE = "/api/admin/privacy";

export const privacyKeys = {
  rules: ["admin", "privacy", "rules"] as const,
  profiles: ["admin", "privacy", "profiles"] as const,
};

function retryUnlessRejected(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

export function usePrivacyRules() {
  return useQuery({
    queryKey: privacyKeys.rules,
    queryFn: () => request<Page<PrivacyRule>>(`${BASE}/rules?limit=200`),
    retry: retryUnlessRejected,
  });
}

export function usePrivacyProfiles() {
  return useQuery({
    queryKey: privacyKeys.profiles,
    queryFn: () => request<Page<PrivacyProfile>>(`${BASE}/profiles?limit=200`),
    retry: retryUnlessRejected,
  });
}

/** A rule changes the profiles' "in use" counts as well, so both are refreshed. */
function useRefresh() {
  const client = useQueryClient();
  return () => Promise.all([
    client.invalidateQueries({ queryKey: privacyKeys.rules }),
    client.invalidateQueries({ queryKey: privacyKeys.profiles }),
  ]);
}

export function useCreatePrivacyRule() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: PrivacyRuleInput) =>
      request<PrivacyRule>(`${BASE}/rules`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useUpdatePrivacyRule() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PrivacyRuleUpdate }) =>
      request<PrivacyRule>(`${BASE}/rules/${id}`, { method: "PATCH", body }),
    onSuccess: refresh,
  });
}

export function useDeletePrivacyRule() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE}/rules/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

export function useReorderPrivacyRules() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { kind: PrivacyKind; order: string[] }) =>
      request<Page<PrivacyRule>>(`${BASE}/rules/reorder`, { method: "POST", body }),
    onSuccess: (page) => client.setQueryData(privacyKeys.rules, page),
  });
}

/** The warnings a draft would get if saved now. Never saves. */
export function useCheckPrivacyRule() {
  return useMutation({
    mutationFn: (body: {
      kind: PrivacyKind;
      name: string;
      enabled: boolean;
      observe_only: boolean;
      conditions: PrivacyCondition[];
      action: PrivacyAction;
      rule_id?: string;
    }) => request<PrivacyRuleCheck>(`${BASE}/rules/check`, { method: "POST", body }),
  });
}

export function useCreatePrivacyProfile() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: PrivacyProfileInput) =>
      request<PrivacyProfile>(`${BASE}/profiles`, { method: "POST", body }),
    onSuccess: refresh,
  });
}

export function useUpdatePrivacyProfile() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PrivacyProfileUpdate }) =>
      request<PrivacyProfile>(`${BASE}/profiles/${id}`, { method: "PATCH", body }),
    onSuccess: refresh,
  });
}

export function useDeletePrivacyProfile() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: string) => request<void>(`${BASE}/profiles/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
}

/** The test box. The text and the file are judged in memory and never stored. */
export function usePrivacyTest() {
  return useMutation({
    mutationFn: (body: PrivacyTestInput) =>
      request<PrivacyTestResult>(`${BASE}/test`, { method: "POST", body }),
  });
}

// -- vocabulary ----------------------------------------------------------------

export const KINDS: PrivacyKind[] = ["read", "embed"];

export function kindLabel(kind: string): string {
  switch (kind) {
    case "read":
      return m["privacy.kind.read"]();
    case "embed":
      return m["privacy.kind.embed"]();
    default:
      return m["privacy.kind.chat"]();
  }
}

/** The condition keys each kind offers, in the order the builder lists them. */
export const CONDITION_KEYS: Record<PrivacyKind, string[]> = {
  read: ["filetype", "filename", "who"],
  embed: ["verdict", "lang", "who", "target"],
};

/** Keys decided by reading the request's own text: they carry "how to decide". */
export function isContentKey(key: string): boolean {
  return key === "verdict" || key === "lang";
}

export const FILETYPES = ["pdf", "word", "excel", "powerpoint", "image", "text", "scan"];
export const VERDICTS = ["health", "ids"];
export const LANGUAGES = ["it", "en", "fr", "de", "es"];
export const READERS = ["markitdown", "ocr", "docling"];
export const PATTERNS = "patterns";

export function conditionLabel(key: string): string {
  switch (key) {
    case "filetype":
      return m["privacy.condition.filetype"]();
    case "filename":
      return m["privacy.condition.filename"]();
    case "who":
      return m["privacy.condition.who"]();
    case "target":
      return m["privacy.condition.target"]();
    case "verdict":
      return m["privacy.condition.verdict"]();
    default:
      return m["privacy.condition.lang"]();
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
      return m["privacy.filetype.scan"]();
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

export function verdictLabel(value: string): string {
  return value === "health" ? m["privacy.verdict.health"]() : m["privacy.verdict.ids"]();
}

export function readerLabel(value: string): string {
  switch (value) {
    case "markitdown":
      return m["privacy.reader.markitdown"]();
    case "ocr":
      return m["privacy.reader.ocr"]();
    default:
      return m["privacy.reader.docling"]();
  }
}

/** One value of a condition, in words. */
export function valueLabel(key: string, value: string): string {
  switch (key) {
    case "filetype":
      return filetypeLabel(value);
    case "verdict":
      return verdictLabel(value);
    case "lang":
      return languageLabel(value);
    case "who":
      return value.startsWith("group:")
        ? m["privacy.who.group"]({ name: value.slice(6) })
        : value.replace(/^user:/, "");
    case "target":
      if (value === "cloud") return m["privacy.target.cloud"]();
      if (value === "local") return m["privacy.target.local"]();
      if (value.startsWith("provider:"))
        return m["privacy.target.provider"]({ name: value.slice(9) });
      return value.replace(/^model:/, "");
    default:
      return value;
  }
}

/** The profile names by id, for the sentence; the quick check has none. */
export type ProfileNames = ReadonlyMap<string, string>;

/** A condition or action as the sentence needs it: written (request) or saved (response). */
interface PhraseCondition {
  key: string;
  values: string[];
  negate?: boolean;
  decide?: string | null;
}
interface PhraseAction {
  type: string;
  reader?: string | null;
}

export function conditionPhrase(condition: PhraseCondition, profiles: ProfileNames): string {
  const values = condition.values.map((v) => valueLabel(condition.key, v)).join(m["privacy.or"]());
  const negate = condition.negate;
  let phrase: string;
  switch (condition.key) {
    case "filetype":
      phrase = (negate ? m["privacy.phrase.filetype_not"] : m["privacy.phrase.filetype"])({ values });
      break;
    case "filename":
      phrase = (negate ? m["privacy.phrase.filename_not"] : m["privacy.phrase.filename"])({ values });
      break;
    case "who":
      phrase = (negate ? m["privacy.phrase.who_not"] : m["privacy.phrase.who"])({ values });
      break;
    case "target":
      phrase = (negate ? m["privacy.phrase.target_not"] : m["privacy.phrase.target"])({ values });
      break;
    case "verdict":
      phrase = (negate ? m["privacy.phrase.verdict_not"] : m["privacy.phrase.verdict"])({ values });
      break;
    default:
      phrase = (negate ? m["privacy.phrase.lang_not"] : m["privacy.phrase.lang"])({ values });
  }
  const profile = condition.decide && condition.decide !== PATTERNS ? profiles.get(condition.decide) : null;
  return profile ? m["privacy.phrase.with_model"]({ phrase, profile }) : phrase;
}

export function actionPhrase(kind: PrivacyKind | string, action: PhraseAction): string {
  if (action.type === "reader") return readerLabel(action.reader ?? "markitdown");
  if (action.type === "local") return m["privacy.action.local"]();
  return kind === "read" ? m["privacy.action.refuse_read"]() : m["privacy.action.refuse_embed"]();
}

/** A rule as a sentence: the same words in the list, the editor and the test result. */
export function ruleSentence(
  rule: { kind: string; conditions: PhraseCondition[]; action: PhraseAction },
  profiles: ProfileNames = new Map(),
): string {
  const action = actionPhrase(rule.kind, rule.action);
  if (rule.conditions.length === 0) {
    return m["privacy.sentence.always"]({
      when: rule.kind === "read" ? m["privacy.when_any.read"]() : m["privacy.when_any.embed"](),
      action,
    });
  }
  const when = rule.conditions
    .map((c) => conditionPhrase(c, profiles))
    .join(m["privacy.and"]());
  return m["privacy.sentence.when"]({ when, action });
}

/** Warning codes that become a badge of their own on the list. */
export function issueBadges(rule: PrivacyRule): {
  never: boolean;
  partly: boolean;
  check: boolean;
} {
  const codes = rule.issues.map((i) => i.code);
  return {
    never: codes.includes("never_fires"),
    partly: codes.includes("partly_shadowed"),
    check: codes.some((c) => c !== "never_fires" && c !== "partly_shadowed"),
  };
}
