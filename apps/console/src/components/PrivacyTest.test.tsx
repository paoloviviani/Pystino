import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { PrivacyTestResult } from "../lib/types";
import { TestResult } from "./PrivacyTest";

/** The Test result: every pipeline in scope with every rule, the reroute, who decided, and which rule claimed which text. */

const BASE: PrivacyTestResult = {
  kind: "extract",
  outcome: "read_with",
  outcome_headline: "Read by docling",
  outcome_text: "The file is read by the reader chosen.",
  level: "extract_with",
  pipelines: [
    {
      id: "p1",
      name: "Admin",
      owner: "admin",
      hop: 0,
      scope: ["all users", "all targets"],
      steps: [
        { rule_id: "r1", name: "Scans", position: 0, result: "matched", reasons: ["The file is a scan."], degraded: [], items: [], policies: [], mode: "off", overrides: {}, note: null },
        { rule_id: "r2", name: "Big files", position: 1, result: "not_matched", reasons: [], degraded: [], items: [], policies: [], mode: "off", overrides: {}, note: null },
      ],
      summary: [],
    },
    {
      id: "p2",
      name: "Mine",
      owner: "user",
      hop: 0,
      scope: ["only you", "all targets"],
      steps: [
        { rule_id: "r3", name: "Mine too", position: 0, result: "matched", reasons: [], degraded: [], items: [], policies: ["Default"], mode: "redact", overrides: {}, note: null },
      ],
      summary: [],
    },
  ],
  skipped: [{ id: "p3", name: "Elsewhere", owner: "admin", why: "the target is not covered" }],
  hops: [{ reader: "docling", reader_name: "docling", added: ["Guard"] }],
  winner_rule: "Scans",
  winner_pipeline: "Admin",
  winner_owner: "admin",
  reader: "docling",
  reader_by: "administrator pipeline Admin",
  reader_overridden: [],
  impossible: null,
  policies: ["Default"],
  claims: [
    { pipeline: "Admin", rule: "Names", rule_id: null, owner: "admin", type: "PERSON", group: "entity", mode: "anonymise_restore", action: "hidden", engine: "spacy", score: 0.9, text: "Mario Rossi" },
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
    expect(screen.getByText("Extractor changed to docling")).toBeInTheDocument();
    expect(screen.getByText("Pipelines for that extractor also checked: Guard.")).toBeInTheDocument();
  });

  it("shows every rule of a pipeline, matched or not, and names who decided", () => {
    show(BASE);
    expect(screen.getByText(/Big files/)).toBeInTheDocument();
    expect(screen.getByText("Decided by Admin › Scans")).toBeInTheDocument();
    expect(screen.getByText("Default → Redact")).toBeInTheDocument();
  });

  it("says who chose the reader", () => {
    show(BASE);
    const reader = screen.getByRole("region", { name: "Reader" });
    expect(within(reader).getByText(/Reader: docling/)).toBeInTheDocument();
    expect(within(reader).getAllByText(/chosen by administrator pipeline Admin/).length).toBe(1);
  });

  it("says which rule claimed which text, with the mode it used", () => {
    show(BASE);
    const claims = screen.getByRole("region", { name: "Hidden text, by rule" });
    const row = within(claims).getByText("Person name").closest("tr") as HTMLElement;
    expect(within(row).getByText(/Admin › Names/)).toBeInTheDocument();
    expect(within(row).getByText("Anonymise, restore in the answer")).toBeInTheDocument();
    expect(within(row).getByText("Mario Rossi")).toBeInTheDocument();
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

  it("says nothing was hidden when no rule claimed anything", () => {
    show({ ...BASE, claims: [], outcome: "pass", level: "default" });
    expect(screen.getByText("Nothing was hidden or recorded.")).toBeInTheDocument();
  });

  it("does not list what a blocked request would have hidden", () => {
    show({ ...BASE, kind: "content", outcome: "block", outcome_headline: "Blocked (403)", level: "block", claims: [] });
    expect(screen.getByText("Not applied: blocked.")).toBeInTheDocument();
  });
});
