import { describe, expect, it } from "vitest";

import {
  conditionKeys,
  issueBadges,
  ruleBody,
  ruleSentence,
  scopeWords,
  summariseDefinition,
  withModes,
  withoutModes,
} from "./privacy";
import type { PolicyDefinition, PrivacyPipeline, PrivacyRuleView } from "./types";

const labels = { pol1: "Default", pol2: "Strict", cl1: "Unannounced project" };

function rule(overrides: Record<string, unknown> = {}): PrivacyRuleView {
  return {
    id: "r1",
    name: "Names found",
    enabled: true,
    note: null,
    policies: ["pol2"],
    match: "all",
    conditions: [],
    mode: "block",
    overrides: {},
    action: null,
    issues: [],
    ...overrides,
  } as PrivacyRuleView;
}

describe("conditionKeys", () => {
  it("offers only the file facts on an extract, and nothing about health, ids or language", () => {
    const all = [...conditionKeys("extract", false), ...conditionKeys("content", false)];
    for (const gone of ["health", "ids", "lang"]) expect(all).not.toContain(gone);
    expect(conditionKeys("extract", false)).toEqual(["filetype", "scan", "filename"]);
  });

  it("offers finds and classified on content, and classified only to administrators", () => {
    expect(conditionKeys("content", false)).toEqual(["finds", "classified"]);
    expect(conditionKeys("content", true)).toEqual(["finds"]);
    expect(conditionKeys("extract", true)).not.toContain("classified");
  });
});

describe("ruleSentence", () => {
  it("says a block over its policy in one line", () => {
    expect(ruleSentence("content", rule(), labels)).toBe("When Strict → block");
  });

  it("joins conditions with the match word and counts the overrides", () => {
    const text = ruleSentence(
      "content",
      rule({
        policies: ["pol1", "pol2"],
        match: "any",
        mode: "anonymise_restore",
        overrides: { IT_VAT_CODE: "redact" },
        conditions: [
          { key: "finds", values: [], negate: false, count: 3 },
          { key: "classified", values: ["cl1"], negate: false, count: 1 },
        ],
      }),
      labels,
    );
    expect(text).toBe(
      "When Default + Strict + it finds at least 3 items or it is classified as Unannounced project → anonymise, restore in the answer (1 override)",
    );
  });

  it("names the extractor of an extract rule", () => {
    const text = ruleSentence(
      "extract",
      rule({
        policies: [],
        mode: "off",
        conditions: [{ key: "filename", values: ["*draft*"], negate: false }],
        action: { level: "extract_with", reader: "markitdown-ocr" },
      }),
      labels,
    );
    expect(text).toBe("When its name matches *draft* → extract with markitdown-ocr");
  });

  it("says 'any file' for no conditions and adds the hiding of an extract rule", () => {
    const text = ruleSentence(
      "extract",
      rule({ mode: "redact", action: { level: "default" } }),
      labels,
    );
    expect(text).toBe("When any file → default extractor, then redact Strict");
  });
});

describe("scopeWords", () => {
  const pipeline = (over: Partial<PrivacyPipeline>): PrivacyPipeline =>
    ({
      id: "p",
      kind: "content",
      name: "P",
      enabled: true,
      position: 0,
      who: { mode: "all", ids: [] },
      target: { mode: "all", providers: [], models: [], model_types: [] },
      rules: [],
      labels: { g1: "Finance", pv1: "OpenAI" },
      summary: [],
      owner_id: null,
      created_at: "",
      updated_at: "",
      ...over,
    }) as PrivacyPipeline;

  it("says all users and all targets by default", () => {
    expect(scopeWords(pipeline({}), false)).toEqual(["All users", "All targets"]);
  });

  it("names groups, a target that is left out and the model types", () => {
    expect(
      scopeWords(
        pipeline({
          who: { mode: "groups", ids: ["g1"] },
          target: { mode: "except", providers: ["pv1"], models: [], model_types: ["embedding", "rerank"] },
        }),
        false,
      ),
    ).toEqual(["Billing groups: Finance", "All except OpenAI · Embedding + Rerank"]);
  });

  it("is only me for a user's own", () => {
    expect(scopeWords(pipeline({}), true)[0]).toBe("Only me");
  });
});

describe("issueBadges and ruleBody", () => {
  it("flags any warning as a check", () => {
    expect(issueBadges(rule())).toEqual({ check: false });
    expect(
      issueBadges(rule({ issues: [{ code: "migrated", message: "y", rule_id: null, rule_name: null }] })),
    ).toEqual({ check: true });
  });

  it("round-trips a rule into what the save sends, keeping its id", () => {
    const body = ruleBody(
      rule({
        conditions: [{ key: "finds", values: [], negate: true, count: 4 }],
        mode: "anonymise",
        overrides: { PERSON: "redact" },
        note: "Switched off by the migration.",
      }),
    );
    expect(body).toEqual({
      id: "r1",
      name: "Names found",
      enabled: true,
      note: "Switched off by the migration.",
      policies: ["pol2"],
      match: "all",
      mode: "anonymise",
      overrides: { PERSON: "redact" },
      conditions: [{ key: "finds", values: [], negate: true, count: 4 }],
    });
  });
});

describe("a policy without modes", () => {
  const def = {
    engine: null,
    allow_list: ["acme"],
    formats: { IT_VAT_CODE: { placeholder: "tagged", threshold: null } },
    entities: {},
    custom_entities: [],
    patterns: [{ name: "KEY", regex: "k+", placeholder: "tagged" }],
    keywords: [],
  } as unknown as PolicyDefinition;

  it("takes a mode on for the editor and drops it again for the save", () => {
    const withMode = withModes(def);
    expect(withMode.default_mode).toBe("off");
    expect(withMode.patterns[0]).toMatchObject({ mode: "redact" });
    expect(withoutModes(withMode)).toEqual(def);
  });

  it("summarises what it looks for, never what it does", () => {
    expect(summariseDefinition(def)).not.toMatch(/redact|anonymise|block/i);
  });
});
