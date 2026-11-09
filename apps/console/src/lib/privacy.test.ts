import { describe, expect, it } from "vitest";

import {
  actionPhrase,
  conditionKeys,
  issueBadges,
  ruleBody,
  ruleSentence,
  scopeWords,
} from "./privacy";
import type { PrivacyPipeline, PrivacyRuleView } from "./types";

const labels = { pol1: "Default", pol2: "Strict", cl1: "Unannounced project" };

function rule(overrides: Record<string, unknown> = {}): PrivacyRuleView {
  return {
    id: "r1",
    name: "Names found",
    enabled: true,
    observe_only: false,
    note: null,
    conditions: [{ key: "finds", values: ["pol2"], negate: false, count: 1 }],
    action: { level: "refuse", reader: null, engine: null, redact_with: null },
    issues: [],
    ...overrides,
  } as PrivacyRuleView;
}

describe("conditionKeys", () => {
  it("offers file conditions on a read and nothing about health, ids or language", () => {
    const all = [...conditionKeys("read", false), ...conditionKeys("embed", false), ...conditionKeys("chat", false)];
    for (const gone of ["health", "ids", "lang"]) expect(all).not.toContain(gone);
    expect(conditionKeys("read", false)).toEqual(["filetype", "scan", "filename", "finds", "classified"]);
  });

  it("offers finds and classified on embed and chat", () => {
    expect(conditionKeys("embed", false)).toEqual(["finds", "classified"]);
    expect(conditionKeys("chat", false)).toEqual(["finds", "classified"]);
  });

  it("leaves classified to administrators", () => {
    expect(conditionKeys("chat", true)).toEqual(["finds"]);
    expect(conditionKeys("read", true)).not.toContain("classified");
  });
});

describe("ruleSentence", () => {
  it("says a refusal on a finds condition in one line", () => {
    expect(ruleSentence("embed", rule(), labels)).toBe("Strict finds at least 1 items → refuse");
  });

  it("joins conditions with 'and' and names the policy and the classifier", () => {
    const text = ruleSentence(
      "chat",
      rule({
        conditions: [
          { key: "finds", values: ["pol1"], negate: true, count: 3 },
          { key: "classified", values: ["cl1"], negate: false, count: 1 },
        ],
        action: { level: "pass", redact_with: "pol1" },
      }),
      labels,
    );
    expect(text).toBe(
      "Default finds fewer than 3 items and it is classified as Unannounced project → redact with Default",
    );
  });

  it("names the reader and the engine of a read", () => {
    const text = ruleSentence(
      "read",
      rule({
        conditions: [{ key: "filename", values: ["*draft*"], negate: false }],
        action: { level: "read_with", reader: "builtin", engine: "ocr" },
      }),
      labels,
    );
    expect(text).toBe(
      "its name matches *draft* → read with the built-in extractor · engine OCR (markitdown + RapidOCR)",
    );
  });

  it("marks an observe-only rule and says 'any request' for no conditions", () => {
    const text = ruleSentence(
      "embed",
      rule({ conditions: [], observe_only: true, action: { level: "refuse" } }),
      labels,
    );
    expect(text).toBe("any request → refuse (observe only)");
  });
});

describe("actionPhrase", () => {
  it("says what happens anyway for the base level with nothing added", () => {
    expect(actionPhrase("read", { level: "default" }, labels)).toBe("default reader");
    expect(actionPhrase("embed", { level: "pass" }, labels)).toBe("pass");
  });

  it("adds the policy to any non-refuse level", () => {
    expect(actionPhrase("read", { level: "default", redact_with: "pol2" }, labels)).toBe(
      "redact with Strict",
    );
  });
});

describe("scopeWords", () => {
  const pipeline = (over: Partial<PrivacyPipeline>): PrivacyPipeline =>
    ({
      id: "p",
      kind: "embed",
      name: "P",
      enabled: true,
      position: 0,
      who: { mode: "all", ids: [] },
      target: { mode: "all", providers: [], models: [] },
      rules: [],
      otherwise: rule({ id: "o", conditions: [] }),
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

  it("names groups and a target that is left out", () => {
    expect(
      scopeWords(
        pipeline({
          who: { mode: "groups", ids: ["g1"] },
          target: { mode: "except", providers: ["pv1"], models: [] },
        }),
        false,
      ),
    ).toEqual(["Billing groups: Finance", "All except OpenAI"]);
  });

  it("is only me for a user's own", () => {
    expect(scopeWords(pipeline({}), true)[0]).toBe("Only me");
  });
});

describe("issueBadges and ruleBody", () => {
  it("separates shadowing from other warnings", () => {
    const flags = issueBadges(
      rule({
        issues: [
          { code: "never_fires", message: "x", rule_id: null, rule_name: null },
          { code: "migrated", message: "y", rule_id: null, rule_name: null },
        ],
      }),
    );
    expect(flags).toEqual({ never: true, partly: false, check: true });
  });

  it("round-trips a rule into what the save sends, keeping its id", () => {
    const body = ruleBody(
      rule({
        conditions: [{ key: "finds", values: ["pol1"], negate: true, count: 4 }],
        action: { level: "pass", redact_with: "pol1", reader: null, engine: null },
        note: "Switched off by the migration.",
      }),
    );
    expect(body).toEqual({
      id: "r1",
      name: "Names found",
      enabled: true,
      observe_only: false,
      note: "Switched off by the migration.",
      conditions: [{ key: "finds", values: ["pol1"], negate: true, count: 4 }],
      action: { level: "pass", redact_with: "pol1" },
    });
  });
});
