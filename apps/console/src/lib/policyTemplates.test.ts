import { describe, expect, it } from "vitest";
import {
  POLICY_TEMPLATES,
  buildPolicyFromTemplate,
  buildUserEntries,
  skippedNote,
} from "./policyTemplates";
import type { PrivacyEngine, PrivacyEngines } from "./types";

const PATTERNS = [
  "IBAN_CODE", "CREDIT_CARD", "IT_FISCAL_CODE", "IT_VAT_CODE", "EMAIL_ADDRESS", "PHONE_NUMBER",
  "URL", "IP_ADDRESS", "MAC_ADDRESS", "IT_IDENTITY_CARD", "IT_PASSPORT", "IT_DRIVER_LICENSE",
  "ES_NIF", "ES_NIE", "UK_NINO", "UK_NHS", "US_SSN", "US_PASSPORT", "US_DRIVER_LICENSE",
  "US_ITIN", "PL_PESEL",
];

const engine = (name: "spacy" | "gliner", over: Partial<PrivacyEngine>): PrivacyEngine => ({
  name,
  available: true,
  default: name === "spacy",
  entities: [],
  custom_labels: name === "gliner",
  labels: [],
  ms_per_token: null,
  languages: [],
  enabled: true,
  used_by: [],
  ...over,
});

const patternOnly: PrivacyEngines = {
  reachable: true,
  detail: "",
  patterns_enabled: true,
  patterns_used_by: [],
  pattern_entities: PATTERNS,
  engines: [engine("spacy", { available: false }), engine("gliner", { available: false })],
};
const spacy: PrivacyEngines = {
  ...patternOnly,
  engines: [
    engine("spacy", { entities: ["PERSON", "LOCATION", "ORGANIZATION", "NRP"] }),
    engine("gliner", { available: false }),
  ],
};
const gliner: PrivacyEngines = {
  ...patternOnly,
  engines: [
    engine("spacy", { entities: ["PERSON", "LOCATION", "ORGANIZATION", "NRP"] }),
    engine("gliner", {
      entities: ["PERSON", "LOCATION", "HEALTH_INSURANCE_ID", "MEDICAL_CONDITION", "MEDICATION"],
    }),
  ],
};

const byId = (id: string) => POLICY_TEMPLATES.find((t) => t.id === id)!;
const entityNames = (e: PrivacyEngines, id: string) =>
  Object.keys(buildPolicyFromTemplate(byId(id), e).policy.entities);

describe("policy templates", () => {
  it("add no entity on a pattern-only service and say what was skipped", () => {
    const built = buildPolicyFromTemplate(byId("personal"), patternOnly);
    expect(built.policy.entities).toEqual({});
    expect(built.policy.engine).toBeNull();
    expect(skippedNote(built.skipped)).toBe("Skipped: PERSON, LOCATION, which need an NER engine.");
    expect(Object.keys(built.policy.formats)).toContain("IBAN_CODE");
  });

  it("add PERSON and LOCATION with spaCy, and not the GLiNER-only health types", () => {
    expect(entityNames(spacy, "personal")).toEqual(["PERSON", "LOCATION"]);
    const health = buildPolicyFromTemplate(byId("health"), spacy);
    expect(health.policy.entities).toEqual({});
    expect(skippedNote(health.skipped)).toBe(
      "Skipped: HEALTH_INSURANCE_ID, MEDICAL_CONDITION, MEDICATION, which need the GLiNER engine.",
    );
  });

  it("add the health types and choose GLiNER only when it is available", () => {
    const health = buildPolicyFromTemplate(byId("health"), gliner);
    expect(Object.keys(health.policy.entities)).toEqual([
      "HEALTH_INSURANCE_ID",
      "MEDICAL_CONDITION",
      "MEDICATION",
    ]);
    expect(health.policy.engine).toBe("gliner");
    expect(health.skipped).toEqual([]);
    expect(Object.values(health.policy.entities).every((i) => i.mode === "redact")).toBe(true);
  });

  it("never add an item the service does not report, whatever the fixture", () => {
    for (const fixture of [patternOnly, spacy, gliner]) {
      for (const template of POLICY_TEMPLATES) {
        const { policy } = buildPolicyFromTemplate(template, fixture);
        for (const name of Object.keys(policy.formats)) {
          expect(fixture.pattern_entities).toContain(name);
        }
        const offered = fixture.engines
          .filter((e) => e.available && (policy.engine ? e.name === policy.engine : e.default))
          .flatMap((e) => e.entities);
        for (const name of Object.keys(policy.entities)) expect(offered).toContain(name);
      }
    }
  });

  it("add a format only when the service lists it", () => {
    const fewer = { ...patternOnly, pattern_entities: ["EMAIL_ADDRESS"] };
    const built = buildPolicyFromTemplate(byId("contact"), fewer);
    expect(Object.keys(built.policy.formats)).toEqual(["EMAIL_ADDRESS"]);
    expect(skippedNote(built.skipped)).toBe(
      "Skipped: PHONE_NUMBER, URL, which the redaction service does not offer.",
    );
  });

  it("add nothing the service must confirm when it cannot be reached", () => {
    const built = buildPolicyFromTemplate(byId("government"), null);
    expect(built.policy.formats).toEqual({});
    // The credential patterns are the console's own and need no service.
    expect(buildPolicyFromTemplate(byId("secrets"), null).policy.patterns.length).toBeGreaterThan(0);
  });

  it("block private keys and redact the other credentials", () => {
    const { patterns } = buildPolicyFromTemplate(byId("secrets"), patternOnly).policy;
    expect(patterns.find((p) => p.name === "PRIVATE_KEY")?.mode).toBe("block");
    expect(patterns.filter((p) => p.name !== "PRIVATE_KEY").every((p) => p.mode === "redact")).toBe(true);
  });

  it("start the internal hosts list empty, for the administrator to fill", () => {
    const { keywords } = buildPolicyFromTemplate(byId("network"), patternOnly).policy;
    expect(keywords).toMatchObject([{ name: "INTERNAL_HOSTS", words: [], mode: "anonymise" }]);
  });

  it("give card and IBAN a same-length style", () => {
    const { formats } = buildPolicyFromTemplate(byId("personal"), patternOnly).policy;
    expect(formats.IBAN_CODE?.placeholder).toBe("same_length");
    expect(formats.CREDIT_CARD?.placeholder).toBe("same_length");
  });
});

describe("policy templates for a user's own policy", () => {
  it("keep to the types the administrator allows", () => {
    const built = buildUserEntries(byId("personal"), {
      allowedTypes: ["IBAN_CODE", "EMAIL_ADDRESS"],
      nerAvailable: true,
    });
    expect(built.entries.map((e) => e.name)).toEqual(["IBAN_CODE", "EMAIL_ADDRESS"]);
    expect(skippedNote(built.skipped)).toContain("your administrator has not made available");
  });

  it("offer no model-backed type without an NER engine, and no keyword list ever", () => {
    const personal = buildUserEntries(byId("personal"), { allowedTypes: null, nerAvailable: false });
    expect(personal.entries.map((e) => e.name)).not.toContain("PERSON");
    expect(skippedNote(personal.skipped)).toBe("Skipped: PERSON, LOCATION, which need an NER engine.");
    const network = buildUserEntries(byId("network"), { allowedTypes: null, nerAvailable: true });
    expect(network.entries.some((e) => e.name === "INTERNAL_HOSTS")).toBe(false);
    expect(skippedNote(network.skipped)).toContain("INTERNAL_HOSTS");
  });

  it("write patterns as the user's own patterns", () => {
    const secrets = buildUserEntries(byId("secrets"), { allowedTypes: [], nerAvailable: null });
    expect(secrets.entries.every((e) => e.kind === "pattern" && !!e.regex)).toBe(true);
  });
});
