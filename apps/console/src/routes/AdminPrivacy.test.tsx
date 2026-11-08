import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrivacyProfile, PrivacyRule } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacy } from "./AdminPrivacy";
import { AdminPrivacyRule } from "./AdminPrivacyRule";

/**
 * The rules list and the rule editor: grouped by kind, a badge for what needs a
 * second look, and the writes the controls make.
 */

function rule(overrides: Partial<PrivacyRule> & { id: string; name: string }): PrivacyRule {
  return {
    kind: "read",
    enabled: true,
    observe_only: false,
    position: 0,
    conditions: [],
    action: { type: "refuse", reader: null, model: null },
    issues: [],
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
    ...overrides,
  };
}

const RULES: PrivacyRule[] = [
  rule({
    id: "r1",
    name: "Medical PDFs",
    position: 0,
    conditions: [{ key: "filename", values: ["referto*"], negate: false, decide: null }],
    action: { type: "reader", reader: "ocr", model: null },
  }),
  rule({
    id: "r2",
    name: "All PDFs",
    position: 1,
    conditions: [{ key: "filetype", values: ["pdf"], negate: false, decide: null }],
    action: { type: "reader", reader: "markitdown", model: null },
    issues: [
      {
        code: "never_fires",
        message: "'Everything' above already catches everything this rule does.",
        rule_id: null,
        rule_name: "Everything",
      },
    ],
  }),
  rule({ id: "r3", name: "Trial", position: 2, observe_only: true, enabled: false }),
  rule({
    id: "e1",
    name: "Health stays out",
    kind: "embed",
    position: 0,
    conditions: [
      { key: "verdict", values: ["health"], negate: false, decide: "patterns" },
      { key: "target", values: ["cloud"], negate: false, decide: null },
    ],
    action: { type: "refuse", reader: null, model: null },
    issues: [
      { code: "unavailable_now", message: "No local embedding provider is set up.", rule_id: null, rule_name: null },
    ],
  }),
];

const PROFILE = {
  id: "p1",
  name: "Fast",
  description: "",
  engine: "presidio",
  labels: [],
  fallback: "refuse",
  fallback_profile_id: null,
  fallback_profile_name: null,
  is_default: true,
  status: "available",
  status_detail: "ok",
  decides: true,
  cost_hint: "instant",
  rules_using: 0,
  created_at: "2026-11-08T10:00:00Z",
  updated_at: "2026-11-08T10:00:00Z",
} as PrivacyProfile;

interface Write {
  method: string;
  url: string;
  body: unknown;
}

function stub(rules: PrivacyRule[], writes: Write[], checkIssues: unknown[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
      }
      if (url.includes("/privacy/rules/check")) {
        return jsonResponse({ error: null, issues: checkIssues, soft_cap: 20 });
      }
      if (url.includes("/privacy/rules/reorder")) {
        return jsonResponse({ items: rules, total: rules.length, limit: 50, offset: 0 });
      }
      if (url.includes("/privacy/rules")) {
        if (method === "POST") return jsonResponse(rule({ id: "new", name: "x" }), 201);
        if (method === "PATCH") return jsonResponse(rules[0]);
        if (method === "DELETE") return new Response(null, { status: 204 });
        return jsonResponse(rules);
      }
      if (url.includes("/privacy/profiles")) return jsonResponse([PROFILE]);
      if (url.includes("/api/admin/groups")) {
        return jsonResponse([{ id: "g1", name: "Clinic", models: [] }]);
      }
      return jsonResponse([]);
    }),
  );
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/admin/privacy" element={<AdminPrivacy />} />
          <Route path="/admin/privacy/rules/:ruleId" element={<AdminPrivacyRule />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the rules list", () => {
  it("groups rules by kind, each as a sentence", async () => {
    stub(RULES, []);
    renderAt("/admin/privacy");

    const reading = (await screen.findByRole("heading", { name: "Reading a file" })).closest("section")!;
    expect(within(reading).getByText("Medical PDFs")).toBeInTheDocument();
    expect(
      within(reading).getByText(
        "When its name matches referto*: read it on this server, with OCR for scans.",
      ),
    ).toBeInTheDocument();
    const embedding = screen.getByRole("heading", { name: "Embedding" }).closest("section")!;
    expect(within(embedding).getByText("Health stays out")).toBeInTheDocument();
    expect(
      within(embedding).getByText(
        "When it contains health data and it goes to a cloud provider: refuse to embed it.",
      ),
    ).toBeInTheDocument();
    // The muted last line, and the note that chat is later.
    expect(within(reading).getByText(/Everything else: handled as it is today/)).toBeInTheDocument();
    expect(screen.getByText(/Rules for chat arrive in a later release/)).toBeInTheDocument();
  });

  it("badges what needs a look, and says why when the badge is tapped", async () => {
    const user = userEvent.setup();
    stub(RULES, []);
    renderAt("/admin/privacy");
    await screen.findByText("All PDFs");

    const never = screen.getByRole("button", { name: "Never fires" });
    expect(never).toHaveAttribute("aria-expanded", "false");
    await user.click(never);
    expect(screen.getByText(/'Everything' above already catches everything/)).toBeInTheDocument();

    // A rule that is off and observe-only wears both.
    expect(screen.getByRole("button", { name: "Off" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Observe" }));
    expect(screen.getByText(/only recorded in the log when it would apply/)).toBeInTheDocument();

    // Any other warning is a "Check".
    await user.click(screen.getByRole("button", { name: "Check" }));
    expect(screen.getByText("No local embedding provider is set up.")).toBeInTheDocument();
  });

  it("moves a rule with the buttons and sends the whole new order", async () => {
    const user = userEvent.setup();
    const writes: Write[] = [];
    stub(RULES, writes);
    renderAt("/admin/privacy");
    await screen.findByText("All PDFs");

    await user.click(screen.getByRole("button", { name: "Move All PDFs up" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      method: "POST",
      body: { kind: "read", order: ["r2", "r1", "r3"] },
    });
    // The first row cannot go up and the last cannot go down.
    expect(screen.getByRole("button", { name: "Move Medical PDFs up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Trial down" })).toBeDisabled();
  });

  it("switches a rule on or off", async () => {
    const user = userEvent.setup();
    const writes: Write[] = [];
    stub(RULES, writes);
    renderAt("/admin/privacy");
    await screen.findByText("Medical PDFs");

    await user.click(screen.getByRole("checkbox", { name: "Medical PDFs is on" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      method: "PATCH",
      url: expect.stringContaining("/privacy/rules/r1"),
      body: { enabled: false },
    });
  });

  it("warns, without blocking, past twenty rules", async () => {
    const many = Array.from({ length: 21 }, (_, n) =>
      rule({ id: `r${n}`, name: `Rule ${n}`, position: n }),
    );
    stub(many, []);
    renderAt("/admin/privacy");
    expect(await screen.findByText(/More than 20 rules is hard to read aloud/)).toBeInTheDocument();
  });

  it("explains how rules work in an overlay", async () => {
    const user = userEvent.setup();
    stub(RULES, []);
    renderAt("/admin/privacy");
    await screen.findByText("Medical PDFs");
    await user.click(screen.getByRole("button", { name: "How rules work" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/The first rule that matches applies/)).toBeInTheDocument();
    expect(within(dialog).getByText(/a renamed file is not caught/)).toBeInTheDocument();
  });

  it("starts a new rule of the kind whose Add rule was pressed", async () => {
    const user = userEvent.setup();
    stub(RULES, []);
    renderAt("/admin/privacy");
    await screen.findByText("Health stays out");
    const embedding = screen.getByRole("heading", { name: "Embedding" }).closest("section")!;
    await user.click(within(embedding).getByRole("button", { name: "Add rule" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/admin/privacy/rules/new?kind=embed");
  });
});

describe("the rule editor", () => {
  it("builds and saves a new rule", async () => {
    const user = userEvent.setup();
    const writes: Write[] = [];
    stub(RULES, writes);
    renderAt("/admin/privacy/rules/new?kind=read");

    await user.type(await screen.findByLabelText("Name"), "Scans here");
    await user.selectOptions(screen.getByLabelText("Add a condition"), "filetype");
    await user.click(screen.getByLabelText("a PDF")); // untick the default
    await user.click(screen.getByLabelText("a scan (no text layer)"));
    await user.selectOptions(screen.getByLabelText("Reader"), "ocr");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(writes.some((w) => w.method === "POST")).toBe(true));
    const post = writes.find((w) => w.method === "POST")!;
    expect(post.url).toMatch(/\/privacy\/rules$/);
    expect(post.body).toEqual({
      kind: "read",
      name: "Scans here",
      enabled: true,
      observe_only: false,
      conditions: [{ key: "filetype", values: ["scan"], negate: false }],
      action: { type: "reader", reader: "ocr" },
    });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/admin/privacy"));
  });

  it("shows the gateway's warnings for the draft, one line each, and does not block saving", async () => {
    const user = userEvent.setup();
    stub(RULES, [], [
      { code: "never_fires", message: "'Medical PDFs' above already catches everything this rule does.", rule_id: null, rule_name: null },
    ]);
    renderAt("/admin/privacy/rules/new");
    await user.type(await screen.findByLabelText("Name"), "Late rule");
    expect(await screen.findByText(/'Medical PDFs' above already catches/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("disables saving, and says what is wrong, when the gateway refuses the draft", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/privacy/rules/check")) {
          return jsonResponse({ error: "'x' needs at least one value.", issues: [], soft_cap: 20 });
        }
        return jsonResponse([]);
      }),
    );
    renderAt("/admin/privacy/rules/new");
    await user.type(await screen.findByLabelText("Name"), "Broken");
    expect(await screen.findByText("'x' needs at least one value.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("edits an existing rule: the kind is fixed, and a change is a PATCH", async () => {
    const user = userEvent.setup();
    const writes: Write[] = [];
    stub(RULES, writes);
    renderAt("/admin/privacy/rules/r1");

    const name = await screen.findByLabelText("Name");
    await waitFor(() => expect(name).toHaveValue("Medical PDFs"));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    await user.click(screen.getByLabelText("Observe only"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(writes.some((w) => w.method === "PATCH")).toBe(true));
    const patch = writes.find((w) => w.method === "PATCH")!;
    expect(patch.url).toMatch(/\/privacy\/rules\/r1$/);
    expect(patch.body).toMatchObject({ observe_only: true, name: "Medical PDFs" });
  });

  it("fills the builder from a template", async () => {
    const user = userEvent.setup();
    stub(RULES, []);
    renderAt("/admin/privacy/rules/new");
    await user.click(await screen.findByRole("button", { name: "Start from a goal" }));
    const dialog = await screen.findByRole("dialog");
    const row = within(dialog).getByText("Never embed health text in the cloud").closest("li")!;
    await user.click(within(row).getByRole("button", { name: "Use" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Health text is never embedded outside");
    expect(screen.getByRole("button", { name: /it contains health data/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Embedding" })).toHaveAttribute("aria-checked", "true");
  });

  it("asks before deleting", async () => {
    const user = userEvent.setup();
    const writes: Write[] = [];
    stub(RULES, writes);
    renderAt("/admin/privacy/rules/r1");
    await screen.findByDisplayValue("Medical PDFs");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(writes).toHaveLength(0);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(writes.some((w) => w.method === "DELETE")).toBe(true));
  });
});
