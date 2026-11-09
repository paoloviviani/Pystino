import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { PrivacyEngines, RedactionPolicy } from "../lib/types";
import { PolicyFields, seededPolicy } from "./PolicyFields";

/**
 * The policy form in isolation: what each section shows for a policy, and what
 * each control writes back. The page's own tests cover it wired to the API.
 */

const ENGINES: PrivacyEngines = {
  reachable: true,
  detail: "",
  patterns_enabled: true,
  patterns_used_by: [],
  pattern_entities: ["EMAIL_ADDRESS", "URL"],
  engines: [
    {
      name: "spacy",
      available: true,
      default: true,
      entities: ["PERSON", "LOCATION"],
      custom_labels: false,
      labels: [],
      enabled: true,
      used_by: [],
      ms_per_token: 0.1,
      languages: ["en", "it"],
    },
    {
      name: "gliner",
      available: true,
      default: false,
      entities: ["PERSON", "ORGANIZATION"],
      custom_labels: true,
      labels: [],
      enabled: true,
      used_by: [],
      ms_per_token: 5,
      languages: ["en"],
    },
  ],
};

function Harness({
  initial = seededPolicy(),
  ...props
}: Partial<React.ComponentProps<typeof PolicyFields>> & { initial?: RedactionPolicy }) {
  const [policy, setPolicy] = useState(initial);
  return (
    <>
      <PolicyFields
        policy={policy}
        onChange={setPolicy}
        engines={ENGINES}
        {...props}
      />
      <output data-testid="policy">{JSON.stringify(policy)}</output>
    </>
  );
}

const written = (): RedactionPolicy =>
  JSON.parse(screen.getByTestId("policy").textContent ?? "{}") as RedactionPolicy;

describe("PolicyFields", () => {
  it("keeps patterns and entities in two separate sections", () => {
    render(<Harness />);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent ?? "");
    expect(headings).toHaveLength(4);
    expect(headings[0]).toContain("Default action");
    expect(headings[1]).toContain("Patterns (fast, deterministic)");
    expect(headings[2]).toContain("Entities (NER, probabilistic)");
    expect(headings[3]).toContain("Never hide");
    // Formats, regular expressions and keywords are in the first; the engine in the second.
    const patterns = screen.getByRole("heading", { name: /^Patterns/ }).closest("section, div")!;
    expect(patterns).toBeTruthy();
    expect(screen.getByLabelText("Engine")).toBeInTheDocument();
  });

  it("changes the default action from a segmented control", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    const control = screen.getByRole("group", { name: "Default action" });
    expect(within(control).getAllByRole("radio")).toHaveLength(6);
    await user.click(within(control).getByRole("radio", { name: "Block" }));
    expect(written().default_mode).toBe("block");
    // Detect only is between Off and Restore.
    expect(
      within(control).getAllByRole("radio").map((r) => (r as HTMLInputElement).value),
    ).toEqual(["off", "detect_only", "anonymise_restore", "anonymise", "redact", "block"]);
    await user.click(within(control).getByRole("radio", { name: "Detect" }));
    expect(written().default_mode).toBe("detect_only");
  });

  it("shows a row for each entity the policy names, in plain words with the id kept", () => {
    render(
      <Harness
        initial={{
          ...seededPolicy(),
          entities: { PERSON: { mode: "block", threshold: 0.7 } },
        }}
      />,
    );

    expect(screen.getByText("Person name")).toBeInTheDocument();
    expect(screen.getByText("PERSON")).toBeInTheDocument();
    const row = screen.getByRole("group", { name: /PERSON/ });
    expect(within(row).getByRole("radio", { name: "Block" })).toBeChecked();
    expect(screen.getByLabelText("Confidence for Person name")).toHaveValue(0.7);
    // The other types are not rows; they are on offer.
    expect(screen.queryByRole("group", { name: /LOCATION/ })).not.toBeInTheDocument();
  });

  it("takes the built-in formats from the service, each with a mode and no confidence", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    await user.selectOptions(screen.getByLabelText("Add a format"), "URL");
    expect(written().formats).toEqual({ URL: { mode: "redact", threshold: null } });
    expect(written().entities).toEqual({});
    expect(screen.queryByLabelText(/^Confidence for/)).not.toBeInTheDocument();
    const row = screen.getByRole("group", { name: /URL/ });
    await user.click(within(row).getByRole("radio", { name: "Detect" }));
    expect(written().formats.URL?.mode).toBe("detect_only");
  });

  it("starts an added type one step above the default and removes it again", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    await user.selectOptions(screen.getByLabelText("Add a type"), "LOCATION");
    expect(written().entities).toEqual({ LOCATION: { mode: "redact", threshold: null } });

    await user.click(screen.getByRole("button", { name: "Reset Place to the default" }));
    expect(written().entities).toEqual({});
  });

  it("starts an added type at Block when the default is already Redact", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness initial={{ ...seededPolicy(), default_mode: "redact" }} />);

    await user.selectOptions(screen.getByLabelText("Add a type"), "PERSON");
    expect(written().entities.PERSON?.mode).toBe("block");
  });

  it("disables the modes below a floor rather than hiding them", () => {
    render(
      <Harness
        initial={{ ...seededPolicy(), entities: { PERSON: { mode: "block", threshold: null } } }}
        floor={{ ...seededPolicy(), default_mode: "redact" }}
      />,
    );

    const row = screen.getByRole("group", { name: /PERSON/ });
    expect(within(row).getByRole("radio", { name: "Off" })).toBeDisabled();
    expect(within(row).getByRole("radio", { name: "Redact" })).toBeEnabled();
  });

  it("is only a heading and two buttons while there are no patterns", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    expect(screen.queryByLabelText("Pattern 1 name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add pattern" }));
    expect(screen.getByLabelText("Pattern 1 name")).toBeInTheDocument();
    expect(written().patterns).toEqual([{ name: "", regex: "", mode: "redact" }]);

    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByLabelText("Pattern 1 name")).not.toBeInTheDocument();
  });

  it("parses the allowlist as it is typed", async () => {
    const user = userEvent.setup({ delay: null });
    render(<Harness />);

    await user.type(screen.getByLabelText("Allowlist"), "intranet.example, example.org");
    expect(written().allow_list).toEqual(["intranet.example", "example.org"]);
  });

  it("leaves the allowlist out where the API refuses one", () => {
    render(<Harness allowList={false} />);
    expect(screen.queryByText("Never hide")).not.toBeInTheDocument();
  });

  describe("keyword lists", () => {
    it("turns the comma-separated words into a list as they are typed", async () => {
      const user = userEvent.setup({ delay: null });
      render(<Harness />);

      await user.click(screen.getByRole("button", { name: "Add keyword list" }));
      expect(written().keywords).toEqual([{ name: "", words: [], case_sensitive: false, mode: "redact" }]);
      await user.type(screen.getByLabelText("Keyword list 1 name"), "Code names");
      await user.type(screen.getByLabelText("Keyword list 1 words"), "alpha, beta gamma ,");
      expect(written().keywords[0]).toMatchObject({
        name: "Code names",
        words: ["alpha", "beta gamma"],
      });
      // The typed line is kept as typed, trailing comma and all.
      expect(screen.getByLabelText("Keyword list 1 words")).toHaveValue("alpha, beta gamma ,");
    });

    it("writes the case-sensitive checkbox, and says what unchecked means in a tooltip", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            keywords: [{ name: "K", words: ["alpha"], case_sensitive: false, mode: "redact" }],
          }}
        />,
      );
      const box = screen.getByRole("checkbox", { name: /Case sensitive/ });
      expect(box).not.toBeChecked();
      expect(
        screen.getByRole("button", {
          name: "Unchecked, capitals and accents are ignored: a word matches however it is written.",
        }),
      ).toBeInTheDocument();
      await user.click(box);
      expect(written().keywords[0]?.case_sensitive).toBe(true);
    });

    it("picks a mode, including detect only, and removes the list", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            keywords: [{ name: "K", words: ["alpha"], case_sensitive: false, mode: "redact" }],
          }}
        />,
      );
      await user.selectOptions(screen.getByLabelText("Keyword list 1 action"), "detect_only");
      expect(written().keywords[0]?.mode).toBe("detect_only");
      await user.click(screen.getAllByRole("button", { name: "Remove" })[0] as HTMLElement);
      expect(written().keywords).toEqual([]);
    });

    it("shows the words of the next list when an earlier one is removed", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            keywords: [
              { name: "A", words: ["one"], case_sensitive: false, mode: "redact" },
              { name: "B", words: ["two", "three"], case_sensitive: false, mode: "redact" },
            ],
          }}
        />,
      );
      await user.click(screen.getAllByRole("button", { name: "Remove" })[0] as HTMLElement);
      expect(screen.getByLabelText("Keyword list 1 words")).toHaveValue("two, three");
    });
  });

  describe("the engine picker", () => {
    it("labels the default engine and lists the entity types of the engine in force", async () => {
      const user = userEvent.setup({ delay: null });
      render(<Harness />);

      const picker = screen.getByLabelText("Engine");
      expect(within(picker).getByRole("option", { name: "Deployment default (spacy)" })).toBeInTheDocument();
      expect(within(picker).getByRole("option", { name: "spacy (default)" })).toBeEnabled();
      // spaCy is the default: its types are on offer, GLiNER's own are not.
      const add = screen.getByLabelText("Add a type");
      expect(within(add).getByRole("option", { name: /Place/ })).toBeInTheDocument();
      expect(within(add).queryByRole("option", { name: /ORGANIZATION/ })).not.toBeInTheDocument();

      await user.selectOptions(picker, "gliner");
      expect(written().engine).toBe("gliner");
      expect(within(screen.getByLabelText("Add a type")).getByRole("option", { name: /ORGANIZATION/ })).toBeInTheDocument();
      await user.selectOptions(picker, "");
      expect(written().engine).toBeNull();
    });

    it("offers only an engine the service reports as available", () => {
      render(
        <Harness
          engines={{
            ...ENGINES,
            engines: ENGINES.engines.map((e) => (e.name === "gliner" ? { ...e, available: false } : e)),
          }}
        />,
      );
      const picker = screen.getByLabelText("Engine");
      expect(within(picker).queryByRole("option", { name: /gliner/ })).not.toBeInTheDocument();
      expect(within(picker).getByRole("option", { name: "spacy (default)" })).toBeEnabled();
    });

    it("offers only the deployment default when the service is unreachable", () => {
      render(
        <Harness
          engines={{ ...ENGINES, reachable: false, detail: "connection refused", pattern_entities: [] }}
        />,
      );
      const picker = screen.getByLabelText("Engine");
      expect(within(picker).getByRole("option", { name: "Deployment default" })).toBeEnabled();
      for (const name of ["spacy", "gliner"]) {
        expect(within(picker).getByRole("option", { name: new RegExp(`^${name}`) })).toBeDisabled();
      }
      expect(screen.getByText("The redaction service did not answer")).toBeInTheDocument();
      expect(screen.getByText("connection refused")).toBeInTheDocument();
    });

    it("keeps an engine the policy names on screen when the service no longer reports it", () => {
      render(<Harness initial={{ ...seededPolicy(), engine: "gliner" }} engines={{ ...ENGINES, engines: [ENGINES.engines[0]!] }} />);
      expect(screen.getByLabelText("Engine")).toHaveValue("gliner");
    });
  });

  describe("custom entity types", () => {
    it("are offered only to an engine that reads labels (GLiNER)", async () => {
      const user = userEvent.setup({ delay: null });
      render(<Harness />);
      expect(screen.queryByRole("button", { name: "Add custom type" })).not.toBeInTheDocument();

      await user.selectOptions(screen.getByLabelText("Engine"), "gliner");
      await user.click(screen.getByRole("button", { name: "Add custom type" }));
      expect(written().custom_entities).toEqual([
        { name: "", label: "", mode: "redact", threshold: null },
      ]);
      await user.type(screen.getByLabelText("Custom type 1 name"), "project code name");
      await user.type(screen.getByLabelText("Custom type 1 label"), "project code name");
      expect(written().custom_entities[0]).toMatchObject({
        name: "PROJECT_CODE_NAME",
        label: "project code name",
      });
    });

    it("are kept, with a warning, when the engine does not read them", () => {
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            custom_entities: [{ name: "PROJECT", label: "project", mode: "redact", threshold: null }],
          }}
        />,
      );
      expect(screen.getByLabelText("Custom type 1 name")).toHaveValue("PROJECT");
      expect(screen.getByText(/does not read custom types/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Add custom type" })).not.toBeInTheDocument();
    });
  });

  describe("placeholder style", () => {
    it("is offered for a type that writes a token, and written back when changed", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: { PERSON: { mode: "anonymise_restore", threshold: null } },
          }}
        />,
      );

      const select = screen.getByLabelText("Placeholder for Person name");
      expect(select).toHaveValue("tagged");
      await user.selectOptions(select, "same_length");
      expect(written().entities.PERSON).toMatchObject({
        mode: "anonymise_restore",
        placeholder: "same_length",
      });
    });

    it("is not offered where there is no token to shape", () => {
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: {
              PERSON: { mode: "redact", threshold: null },
              LOCATION: { mode: "block", threshold: null },
            },
          }}
        />,
      );
      expect(screen.queryByLabelText(/^Placeholder for/)).not.toBeInTheDocument();
    });

    it("is offered on every built-in format row and written back", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            formats: { EMAIL_ADDRESS: { mode: "anonymise", threshold: null } },
          }}
        />,
      );
      const select = screen.getByLabelText(/^Placeholder for .*EMAIL_ADDRESS|^Placeholder for Email/);
      expect(select).toBeEnabled();
      await user.selectOptions(select, "same_length");
      expect(written().formats.EMAIL_ADDRESS?.placeholder).toBe("same_length");
    });

    it("appears on a keyword list and a custom entity once the action writes a token", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            engine: "gliner",
            keywords: [{ name: "CODES", words: ["falcon"], case_sensitive: false, mode: "redact" }],
            custom_entities: [{ name: "PROJECT", label: "project", mode: "redact", threshold: null }],
          }}
        />,
      );
      expect(screen.queryByLabelText("Keyword list 1 placeholder")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Custom type 1 placeholder")).not.toBeInTheDocument();
      await user.selectOptions(screen.getByLabelText("Keyword list 1 action"), "anonymise");
      await user.selectOptions(screen.getByLabelText("Custom type 1 action"), "anonymise_restore");
      await user.selectOptions(screen.getByLabelText("Keyword list 1 placeholder"), "same_length");
      await user.selectOptions(screen.getByLabelText("Custom type 1 placeholder"), "same_length");
      expect(written().keywords[0]?.placeholder).toBe("same_length");
      expect(written().custom_entities[0]?.placeholder).toBe("same_length");
    });

    it("keeps the choice when the mode changes", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            entities: {
              PERSON: {
                mode: "anonymise_restore",
                threshold: null,
                placeholder: "same_length",
              },
            },
          }}
        />,
      );
      const row = screen.getByRole("group", { name: /PERSON/ });
      await user.click(within(row).getByRole("radio", { name: "Block" }));
      expect(written().entities.PERSON?.placeholder).toBe("same_length");
    });

    it("can be chosen for a custom pattern", async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <Harness
          initial={{
            ...seededPolicy(),
            patterns: [
              { name: "BADGE", regex: "B-[0-9]+", mode: "anonymise_restore", placeholder: "tagged" },
            ],
          }}
        />,
      );
      await user.selectOptions(screen.getByLabelText("Pattern 1 placeholder"), "same_length");
      expect(written().patterns[0]?.placeholder).toBe("same_length");
    });
  });
});


/** A service whose image has no NER model: the engines are listed and expose nothing. */
const PATTERN_ONLY: PrivacyEngines = {
  reachable: true,
  detail: "",
  patterns_enabled: true,
  patterns_used_by: [],
  pattern_entities: ["EMAIL_ADDRESS", "IBAN_CODE"],
  engines: [
    {
      name: "spacy",
      available: false,
      default: true,
      entities: [],
      custom_labels: false,
      labels: [],
      enabled: true,
      used_by: [],
      ms_per_token: null,
      languages: [],
    },
    {
      name: "gliner",
      available: false,
      default: false,
      entities: [],
      custom_labels: true,
      labels: [],
      enabled: true,
      used_by: [],
      ms_per_token: null,
      languages: [],
    },
  ],
};

describe("PolicyFields against a pattern-only service", () => {
  it("hides the entities section and says why, with a link to the docs", () => {
    render(<Harness engines={PATTERN_ONLY} />);
    expect(
      screen.getByText(/No NER engine is installed in the redaction service; entities need one\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "How to enable it" })).toHaveAttribute(
      "href",
      expect.stringContaining("/redaction/"),
    );
    expect(screen.getByText(/\.\/configure --redaction/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Engine")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a type")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add custom type" })).not.toBeInTheDocument();
    // The patterns half is untouched.
    expect(screen.getByLabelText("Add a format")).toBeInTheDocument();
  });

  it("keeps entity items the policy already lists, and says so", () => {
    render(
      <Harness
        engines={PATTERN_ONLY}
        initial={{ ...seededPolicy(), entities: { PERSON: { mode: "redact", threshold: null } } }}
      />,
    );
    expect(screen.getByText(/still lists 1 entity items/)).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId("policy").textContent ?? "{}").entities.PERSON.mode).toBe(
      "redact",
    );
  });

  it("shows the section again when the service has an engine", () => {
    render(<Harness engines={ENGINES} />);
    expect(screen.queryByTestId("no-ner")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Engine")).toBeInTheDocument();
  });

  it("offers custom entities only when GLiNER is available", () => {
    const spacyOnly = { ...ENGINES, engines: [ENGINES.engines[0]!] };
    render(<Harness engines={spacyOnly} />);
    expect(screen.queryByRole("button", { name: "Add custom type" })).not.toBeInTheDocument();
  });
});

describe("a format the service does not list (AGE)", () => {
  it("is kept, turned off and flagged, never dropped", () => {
    render(
      <Harness
        initial={{
          ...seededPolicy(),
          formats: { AGE: { mode: "anonymise", threshold: null } },
        }}
      />,
    );
    const policy = JSON.parse(screen.getByTestId("policy").textContent ?? "{}");
    expect(policy.formats.AGE.mode).toBe("off");
    expect(screen.getByText(/no longer offers these formats.*AGE/)).toBeInTheDocument();
    expect(screen.getByText("Not offered by the redaction service. Kept off.")).toBeInTheDocument();
  });

  it("moves to the entities when the engine finds it", () => {
    const withAge: PrivacyEngines = {
      ...ENGINES,
      engines: [{ ...ENGINES.engines[0]!, entities: ["PERSON", "AGE"] }, ENGINES.engines[1]!],
    };
    render(
      <Harness
        engines={withAge}
        initial={{ ...seededPolicy(), formats: { AGE: { mode: "redact", threshold: null } } }}
      />,
    );
    const policy = JSON.parse(screen.getByTestId("policy").textContent ?? "{}");
    expect(policy.formats.AGE).toBeUndefined();
    expect(policy.entities.AGE.mode).toBe("redact");
    expect(screen.getByText(/Moved from formats to entities.*AGE/)).toBeInTheDocument();
  });
});

describe("PolicyFields follows the detectors switched on", () => {
  it("offers no formats when the patterns are switched off", () => {
    render(<Harness engines={{ ...ENGINES, patterns_enabled: false }} />);
    expect(screen.getByLabelText("Add a format")).toBeDisabled();
  });

  it("offers only the engines that are switched on, and the entities of the one in force", () => {
    const gliner = { ...ENGINES.engines[1]!, enabled: false };
    render(<Harness engines={{ ...ENGINES, engines: [ENGINES.engines[0]!, gliner] }} />);
    const picker = screen.getByLabelText("Engine");
    expect(within(picker).queryByRole("option", { name: /gliner/ })).not.toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: "spacy (default)" })).toBeEnabled();
  });

  it("hides the entities, with the reason, when every NER engine is switched off", () => {
    const off = ENGINES.engines.map((e) => ({ ...e, enabled: false }));
    render(<Harness engines={{ ...ENGINES, engines: off }} />);
    expect(screen.getByTestId("no-ner")).toBeInTheDocument();
    expect(screen.queryByLabelText("Engine")).not.toBeInTheDocument();
  });
});
