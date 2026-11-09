import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { PrivacyTestResult } from "../lib/types";
import { TestResult } from "./PrivacyTest";

/** The Test result: every pipeline in scope with its trace, the reroute, the ladder and who won, the policies added up. */

const BASE: PrivacyTestResult = {
  kind: "read",
  outcome: "read_with",
  outcome_headline: "Read here by the built-in extractor (docling)",
  outcome_text: "The file is read by the reader chosen.",
  level: "read_with",
  ladder: [
    { level: "default", label: "Default reader", asked_by: [], winning: false },
    { level: "read_with", label: "Read with the built-in extractor", asked_by: ["Admin"], winning: true },
    { level: "refuse", label: "Refuse", asked_by: ["Strict"], winning: false },
  ],
  pipelines: [
    {
      id: "p1",
      name: "Admin",
      owner: "admin",
      hop: 0,
      scope: ["all users", "all targets"],
      steps: [
        { rule_id: "r1", name: "Scans", position: 0, result: "fired", reasons: ["The file is a scan."], degraded: [], items: [], note: null, is_otherwise: false },
        { rule_id: "e1", name: "Everything else", position: 1, result: "not_reached", reasons: [], degraded: [], items: [], note: null, is_otherwise: true },
      ],
      fired_rule_id: "r1",
      fired_rule: "Scans",
      fired_otherwise: false,
      level: "read_with",
      action_text: "Read with the built-in extractor, engine docling",
      summary: [],
    },
    {
      id: "p2",
      name: "Mine",
      owner: "user",
      hop: 0,
      scope: ["only you", "all targets"],
      steps: [
        { rule_id: "e2", name: "Everything else", position: 0, result: "fired", reasons: [], degraded: [], items: [], note: null, is_otherwise: true },
      ],
      fired_rule_id: "e2",
      fired_rule: null,
      fired_otherwise: true,
      level: "read_with",
      action_text: "Read with the built-in extractor, engine ocr",
      summary: [],
    },
  ],
  skipped: [{ id: "p3", name: "Elsewhere", owner: "admin", why: "the target is not covered" }],
  hops: [{ reader: "builtin", reader_name: "the built-in extractor", added: ["Guard"] }],
  winner_rule: "Scans",
  winner_pipeline: "Admin",
  winner_owner: "admin",
  reader: "the built-in extractor",
  reader_by: "administrator pipeline Admin",
  reader_overridden: [],
  engine: "docling",
  engine_by: "administrator pipeline Admin",
  impossible: null,
  policies: ["Default", "Mine"],
  policy_types: [
    { entity_type: "PERSON", mode: "redact", source: "Mine", per_policy: ["Default: anonymise_restore", "Mine: redact"], found: null },
  ],
  blocked_by: null,
  facts: {},
  verdicts: {},
  degraded: [],
  needs: null,
  extractor: null,
  fallback_reason: null,
  estimate_ms: null,
};

function show(result: PrivacyTestResult) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TestResult result={result} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Test result", () => {
  it("shows every pipeline in scope with its trace, and those that were not", () => {
    show(BASE);
    expect(screen.getByText("Pipelines in scope")).toBeInTheDocument();
    expect(screen.getByText("Admin", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Mine", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("The file is a scan.")).toBeInTheDocument();
    expect(screen.getByText("1 not in scope")).toBeInTheDocument();
  });

  it("names the reroute and the pipelines that ran for the new reader", () => {
    show(BASE);
    expect(screen.getByText("Rerouted to the built-in extractor")).toBeInTheDocument();
    expect(screen.getByText("Pipelines for that reader also checked: Guard.")).toBeInTheDocument();
  });

  it("names the winner on the ladder, the administrator's on a tie", () => {
    show(BASE);
    const ladder = screen.getByRole("region", { name: "Ladder" });
    expect(within(ladder).getByText("wins · Admin › Scans")).toBeInTheDocument();
    // A level that lost still says who asked for it.
    expect(within(ladder).getByText("asked by Strict")).toBeInTheDocument();
  });

  it("says who chose the reader and the engine", () => {
    show(BASE);
    const reader = screen.getByRole("region", { name: "Reader and engine" });
    expect(within(reader).getByText(/Reader: the built-in extractor/)).toBeInTheDocument();
    expect(within(reader).getAllByText(/chosen by administrator pipeline Admin/).length).toBe(2);
    expect(within(reader).getByText(/Engine: docling/)).toBeInTheDocument();
  });

  it("combines the policies: the strictest mode and where it came from", () => {
    show(BASE);
    const policies = screen.getByRole("region", { name: "Policies combined" });
    expect(within(policies).getByText("From Default + Mine")).toBeInTheDocument();
    const row = within(policies).getByText("Person name").closest("tr") as HTMLElement;
    expect(within(row).getByText("Mine")).toBeInTheDocument();
    expect(within(row).getByText(/Default: anonymise_restore; Mine: redact/)).toBeInTheDocument();
  });

  it("lists each item that made a condition true, with its group, type, engine and score", () => {
    const result: PrivacyTestResult = {
      ...BASE,
      pipelines: [
        {
          ...BASE.pipelines[0]!,
          steps: [
            {
              ...BASE.pipelines[0]!.steps[0]!,
              items: [
                { policy: "Contacts", group: "pattern", type: "EMAIL_ADDRESS", engine: null, score: null, text: "ana@example.org" },
                { policy: "Contacts", group: "entity", type: "PERSON", engine: "spacy", score: 0.91, text: null },
              ],
            },
          ],
        },
      ],
    };
    show(result);
    const items = screen.getByRole("list", { name: "Items found" });
    const rows = within(items).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("Contacts")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("pattern")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("Email address")).toBeInTheDocument();
    // The text is only there when the API sent it: the administrator's box.
    expect(within(rows[0]!).getByText("ana@example.org")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("entity")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("spacy · 0.91")).toBeInTheDocument();
    expect(within(rows[1]!).queryByRole("code")).toBeNull();
    expect(within(rows[1]!).getByText("Person name")).toBeInTheDocument();
  });

  it("shows a step's degraded sentence and note", () => {
    const step = {
      ...BASE.pipelines[0]!.steps[0]!,
      degraded: ["The engine did not answer, so the count may be short."],
      note: "Undecidable: the rule did not fire.",
    };
    show({ ...BASE, pipelines: [{ ...BASE.pipelines[0]!, steps: [step] }] });
    expect(screen.getByText("The engine did not answer, so the count may be short.")).toBeInTheDocument();
    expect(screen.getByText("Undecidable: the rule did not fire.")).toBeInTheDocument();
  });

  it("shows classifier verdicts by name", () => {
    show({ ...BASE, verdicts: { "Unannounced project": true, "Other topic": false } });
    const verdicts = screen.getByRole("region", { name: "Classifiers" });
    expect(within(verdicts).getByText("Unannounced project").parentElement).toHaveTextContent("yes");
    expect(within(verdicts).getByText("Other topic").parentElement).toHaveTextContent("no");
  });

  it("has no verdicts section when no classifier ran", () => {
    show(BASE);
    expect(screen.queryByRole("region", { name: "Classifiers" })).toBeNull();
  });

  it("does not show policies for a refused request", () => {
    show({ ...BASE, outcome: "refuse", outcome_headline: "Refused (403)", level: "refuse", policies: [], policy_types: [] });
    expect(screen.getByText("Not applied: refused.")).toBeInTheDocument();
  });
});
