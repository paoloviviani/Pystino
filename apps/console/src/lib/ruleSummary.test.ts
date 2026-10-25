import { describe, expect, it } from "vitest";
import { isWideRoute } from "./layout";
import { summariseRule } from "./ruleSummary";
import type { RedactionPolicy } from "./types";

function policy(overrides: Partial<RedactionPolicy> = {}): RedactionPolicy {
  return {
    default_mode: "off",
    entities: {},
    patterns: [],
    allow_list: [],
    ...overrides,
  };
}

const entity = (mode: RedactionPolicy["default_mode"]) => ({
  mode,
  threshold: null,
});

describe("summariseRule", () => {
  it("says so when the rule does nothing", () => {
    expect(summariseRule(policy())).toBe(
      "Does nothing yet. Every kind of data is left as written.",
    );
  });

  it("names one or two types and counts more, strongest first", () => {
    expect(
      summariseRule(
        policy({
          entities: {
            PERSON: entity("redact"),
            LOCATION: entity("redact"),
            ORGANIZATION: entity("redact"),
            CREDIT_CARD: entity("block"),
          },
          patterns: [
            { name: "A", regex: "a", mode: "redact" },
            { name: "B", regex: "b", mode: "redact" },
          ],
        }),
      ),
    ).toBe("Blocks payment card number, redacts 3 kinds of data, 2 custom patterns.");
  });

  it("joins two names, and keeps a label nobody here knows as it is", () => {
    expect(
      summariseRule(
        policy({
          entities: { IBAN_CODE: entity("block"), TICKET_ID: entity("block") },
        }),
      ),
    ).toBe("Blocks bank account (IBAN) and TICKET_ID.");
  });

  it("reports a default above Off and the types it is lifted from", () => {
    expect(
      summariseRule(policy({ default_mode: "redact", entities: { URL: entity("off") } })),
    ).toBe("Redacts everything else, leaves web address as written.");
    expect(summariseRule(policy({ default_mode: "block" }))).toBe("Blocks everything it finds.");
  });

  it("does not repeat the default for a row set to Off under an Off default", () => {
    expect(summariseRule(policy({ entities: { URL: entity("off") } }))).toBe(
      "Does nothing yet. Every kind of data is left as written.",
    );
  });

  it("counts the allowlist", () => {
    expect(summariseRule(policy({ default_mode: "redact", allow_list: ["a.org"] }))).toBe(
      "Redacts everything it finds, never hides 1 listed value.",
    );
  });
});

describe("an allowlist the gateway ignores", () => {
  const listed = policy({ default_mode: "redact", allow_list: ["a.org"] });

  it("is not promised in the summary", () => {
    const text = summariseRule(listed, true);
    expect(text).not.toContain("never hides");
    expect(text).toBe(
      "Redacts everything it finds, ignores its never-hide list, because the rule for every request has its own.",
    );
  });

  it("says nothing about it when there is no list", () => {
    expect(summariseRule(policy({ default_mode: "block" }), true)).toBe(
      "Blocks everything it finds.",
    );
  });
});

describe("isWideRoute", () => {
  it("is wide for the list and for one rule's page, new or edit", () => {
    expect(isWideRoute("/admin/redaction")).toBe(true);
    expect(isWideRoute("/admin/redaction/rules/new")).toBe(true);
    expect(isWideRoute("/admin/redaction/rules/3f2c/")).toBe(true);
  });

  it("is not wide for a form elsewhere", () => {
    expect(isWideRoute("/admin/settings")).toBe(false);
    expect(isWideRoute("/admin/redaction/rules")).toBe(false);
    expect(isWideRoute("/admin/redaction/rules/a/b")).toBe(false);
  });
});
