import { describe, expect, it } from "vitest";

import {
  actionPhrase,
  issueBadges,
  ruleBody,
  ruleSentence,
  scopeWords,
} from "./privacy";
import type { PrivacyPipeline, PrivacyRuleView } from "./types";

const labels = { pol1: "Default", pol2: "Strict" };

function rule(overrides: Record<string, unknown> = {}): PrivacyRuleView {
  return {
    id: "r1",
    name: "Health text",
    enabled: true,
    observe_only: false,
    conditions: [{ key: "health", values: [], negate: false, decide: "patterns" }],
    action: { level: "refuse", reader: null, engine: null, redact_with: null },
    issues: [],
    ...overrides,
  } as PrivacyRuleView;
}

describe("ruleSentence", () => {
  it("says a refusal on health data in one line", () => {
    expect(ruleSentence("embed", rule(), labels)).toBe("it contains health data → refuse");
  });

  it("joins conditions with 'and' and names the policy", () => {
    const text = ruleSentence(
      "chat",
      rule({
        conditions: [
          { key: "ids", values: [], negate: false },
          { key: "lang", values: ["it"], negate: false },
        ],
        action: { level: "pass", redact_with: "pol1" },
      }),
      labels,
    );
    expect(text).toBe("it contains IDs and it is written in Italian → redact with Default");
  });

  it("names the reader and the engine of a read", () => {
    const text = ruleSentence(
      "read",
      rule({
        conditions: [{ key: "filename", values: ["*referto*"], negate: false }],
        action: { level: "read_with", reader: "builtin", engine: "ocr" },
      }),
      labels,
    );
    expect(text).toBe(
      "its name matches *referto* → read with the built-in extractor · engine OCR (markitdown + RapidOCR)",
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
      labels: { g1: "Clinic", pv1: "OpenAI" },
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
    ).toEqual(["Groups: Clinic", "All except OpenAI"]);
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
          { code: "weakens", message: "y", rule_id: null, rule_name: null },
        ],
      }),
    );
    expect(flags).toEqual({ never: true, partly: false, check: true });
  });

  it("round-trips a rule into what the save sends, keeping its id", () => {
    const body = ruleBody(
      rule({
        conditions: [{ key: "health", values: [], negate: true, decide: "patterns" }],
        action: { level: "pass", redact_with: "pol1", reader: null, engine: null },
      }),
    );
    expect(body).toEqual({
      id: "r1",
      name: "Health text",
      enabled: true,
      observe_only: false,
      conditions: [{ key: "health", values: [], negate: true, decide: "patterns" }],
      action: { level: "pass", redact_with: "pol1" },
    });
  });
});
