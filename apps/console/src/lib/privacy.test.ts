import { describe, expect, it } from "vitest";
import { issueBadges, ruleSentence } from "./privacy";
import type { PrivacyRule } from "./types";

/**
 * The sentence is the rule's one spelling: the list, the editor's subtitle and
 * the test result all read it, so a change here is a change to what an
 * administrator is told their rule does.
 */

describe("ruleSentence", () => {
  it("reads a file rule as a sentence", () => {
    expect(
      ruleSentence({
        kind: "read",
        conditions: [
          { key: "filetype", values: ["pdf"], negate: false },
          { key: "filename", values: ["referto*", "esame*"], negate: false },
        ],
        action: { type: "reader", reader: "ocr" },
      }),
    ).toBe(
      "When it is a PDF and its name matches referto* or esame*: read it on this server, with OCR for scans.",
    );
  });

  it("says is not, and names a profile that decides", () => {
    const names = new Map([["p1", "Medical"]]);
    expect(
      ruleSentence(
        {
          kind: "embed",
          conditions: [
            { key: "verdict", values: ["health"], negate: false, decide: "p1" },
            { key: "target", values: ["local"], negate: true },
          ],
          action: { type: "refuse" },
        },
        names,
      ),
    ).toBe(
      "When it contains health data (decided by Medical) and it does not go to a local model: refuse to embed it.",
    );
  });

  it("describes a rule with no conditions as matching everything", () => {
    expect(ruleSentence({ kind: "embed", conditions: [], action: { type: "local" } })).toBe(
      "Every embedding request: embed it with a local embedding model.",
    );
    expect(ruleSentence({ kind: "read", conditions: [], action: { type: "refuse" } })).toBe(
      "Every file read: refuse to read it.",
    );
  });

  it("names groups and users in words", () => {
    expect(
      ruleSentence({
        kind: "read",
        conditions: [{ key: "who", values: ["group:Clinic", "user:ana@example.org"], negate: false }],
        action: { type: "refuse" },
      }),
    ).toBe("When it is from group Clinic or ana@example.org: refuse to read it.");
  });
});

describe("issueBadges", () => {
  const rule = (codes: string[]): PrivacyRule =>
    ({
      issues: codes.map((code) => ({ code, message: code, rule_id: null, rule_name: null })),
    }) as PrivacyRule;

  it("separates the two shadowing badges from the generic Check", () => {
    expect(issueBadges(rule(["never_fires"]))).toEqual({ never: true, partly: false, check: false });
    expect(issueBadges(rule(["partly_shadowed", "weakens"]))).toEqual({
      never: false,
      partly: true,
      check: true,
    });
    expect(issueBadges(rule([]))).toEqual({ never: false, partly: false, check: false });
  });
});
