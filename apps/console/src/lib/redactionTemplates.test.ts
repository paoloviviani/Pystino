import { describe, expect, it } from "vitest";
import { REDACTION_PATTERN_TEMPLATES } from "./redactionTemplates";

/**
 * The pattern-template catalogue is a safer place than a seeded rule to keep
 * credential shapes. What matters here is that every offer is unique, states
 * its consequence, and avoids regex constructs outside RE2.
 */
describe("redaction pattern templates", () => {
  it("offers unique names and RE2-compatible expressions", () => {
    const names = REDACTION_PATTERN_TEMPLATES.map((template) => template.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toContain("OPENAI_KEY");
    expect(names).toContain("PRIVATE_KEY");

    for (const template of REDACTION_PATTERN_TEMPLATES) {
      expect(template.regex.length).toBeGreaterThan(0);
      // No lookaround, backreferences, or inline flags: none of these can be
      // saved by the API's RE2 validation.
      expect(template.regex).not.toContain("(?");
      expect(template.regex).not.toMatch(/\\[1-9]/);
      // A template that cannot compile in a browser is certainly not one to
      // send to the server for its opinion.
      expect(() => new RegExp(template.regex)).not.toThrow();
      expect(["credential", "identifier"]).toContain(template.kind);
      expect(["block", "redact"]).toContain(template.mode);
    }
  });

  it("blocks credentials and redacts identifiers", () => {
    // Sending a key is itself the incident; an identifier can still leave a
    // useful request behind once it is replaced.
    const modes = new Map(
      REDACTION_PATTERN_TEMPLATES.map((template) => [template.name, template.mode]),
    );
    expect(modes.get("GITHUB_FINE_GRAINED_TOKEN")).toBe("block");
    expect(modes.get("URL_WITH_PASSWORD")).toBe("block");
    expect(modes.get("UUID")).toBe("redact");
    expect(modes.get("DATE_OF_BIRTH")).toBe("redact");
  });
});
