import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrivacyPipeline } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacy } from "./AdminPrivacy";

/** The Pipelines tab: pipelines by kind, each with its scope, its rules as sentences and the fixed last rule. */

function action(level: string, extra: Record<string, unknown> = {}) {
  return { level, reader: null, engine: null, redact_with: null, ...extra };
}

function pipeline(overrides: Partial<PrivacyPipeline> & { id: string; name: string }): PrivacyPipeline {
  return {
    owner_id: null,
    kind: "embed",
    enabled: true,
    position: 0,
    who: { mode: "all", ids: [] },
    target: { mode: "all", providers: [], models: [] },
    rules: [],
    otherwise: {
      id: "else",
      name: "Everything else",
      enabled: true,
      observe_only: false,
      note: null,
      conditions: [],
      action: action("pass"),
      issues: [],
    },
    labels: {},
    summary: [],
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
    ...overrides,
  } as PrivacyPipeline;
}

const REMOTE = pipeline({
  id: "p1",
  name: "Remote embeddings",
  who: { mode: "groups", ids: ["g1"] },
  target: { mode: "only", providers: ["pv1"], models: [] },
  labels: { g1: "Finance", pv1: "OpenAI", pol1: "Default", pol2: "Names" },
  rules: [
    {
      id: "r1",
      name: "Names found",
      enabled: true,
      observe_only: false,
      note: null,
      conditions: [{ key: "finds", values: ["pol2"], negate: false, count: 2 }],
      action: action("refuse"),
      issues: [],
    },
  ],
  otherwise: {
    id: "else1",
    name: "Everything else",
    enabled: true,
    observe_only: false,
    note: null,
    conditions: [],
    action: action("pass", { redact_with: "pol1" }),
    issues: [],
  },
});
const SECOND = pipeline({ id: "p2", name: "Second", position: 1 });
const READ = pipeline({
  id: "p3",
  kind: "read",
  name: "Reports",
  rules: [
    {
      id: "r3",
      name: "Scans",
      enabled: true,
      observe_only: false,
      note: null,
      conditions: [{ key: "scan", values: [], negate: false, count: 1 }],
      action: action("read_with", { reader: "builtin", engine: "docling" }),
      issues: [{ code: "never_fires", message: "above catches it", rule_id: null, rule_name: null }],
    },
  ],
  otherwise: {
    id: "else3",
    name: "Everything else",
    enabled: true,
    observe_only: false,
    note: null,
    conditions: [],
    action: action("default"),
    issues: [],
  },
});

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function api(seen: Seen[], items: PrivacyPipeline[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.includes("/pipelines/reorder")) return jsonResponse({ items, total: items.length });
    if (url.includes("/pipelines") && method === "GET") {
      return jsonResponse({ items, total: items.length, limit: 200, offset: 0 });
    }
    if (url.includes("/pipelines") && method === "DELETE") return new Response(null, { status: 204 });
    if (url.includes("/pipelines")) return jsonResponse(items[0]);
    return jsonResponse({});
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPrivacy />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Pipelines", () => {
  it("has the five tabs and no 'local' anywhere", async () => {
    vi.stubGlobal("fetch", api([], []));
    renderPage();
    const nav = await screen.findByRole("navigation", { name: "Privacy sections" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Pipelines",
      "Redaction policies",
      "Classifiers",
      "Detectors",
      "Test",
    ]);
    expect(screen.queryByText(/local/i)).toBeNull();
  });

  it("lists each pipeline under its kind with scope badges and its rules as sentences", async () => {
    vi.stubGlobal("fetch", api([], [REMOTE, SECOND, READ]));
    renderPage();

    const embed = await screen.findByRole("region", { name: "Embedding" });
    expect(within(embed).getByText("Remote embeddings")).toBeInTheDocument();
    expect(within(embed).getByText("Billing groups: Finance")).toBeInTheDocument();
    expect(within(embed).getByText("Only OpenAI")).toBeInTheDocument();
    expect(within(embed).getByText("Names finds at least 2 items → refuse")).toBeInTheDocument();
    // The fixed last rule is always shown, and names the policy it applies.
    expect(within(embed).getByText("everything else → redact with Default")).toBeInTheDocument();

    const read = screen.getByRole("region", { name: "Reading a file" });
    expect(
      within(read).getByText(/the file is a scan → read with the built-in extractor · engine Docling/),
    ).toBeInTheDocument();
    // A rule that never fires is flagged on its pipeline.
    expect(within(read).getByText("Check")).toBeInTheDocument();
    // Chat has none yet, and says requests are handled as ever.
    expect(
      within(screen.getByRole("region", { name: "Chat" })).getByText(
        "No pipelines of this kind",
      ),
    ).toBeInTheDocument();
  });

  it("shows a note the migration left on a rule as a warning on the pipeline", async () => {
    const migrated = pipeline({
      id: "p4",
      name: "Migrated",
      rules: [
        {
          id: "r4",
          name: "Old rule",
          enabled: false,
          observe_only: false,
          note: "Switched off by the migration: it used a check that no longer exists.",
          conditions: [],
          action: action("refuse"),
          issues: [{ code: "migrated", message: "Switched off by the migration.", rule_id: "r4", rule_name: "Old rule" }],
        },
      ],
    });
    vi.stubGlobal("fetch", api([], [migrated]));
    renderPage();
    expect(
      await screen.findByText("Switched off by the migration: it used a check that no longer exists."),
    ).toBeInTheDocument();
    expect(screen.getByText("Check")).toBeInTheDocument();
  });

  it("moves a pipeline within its kind by sending the whole order", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen, [REMOTE, SECOND]));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Move Remote embeddings down" }));

    await waitFor(() =>
      expect(seen.find((c) => c.url.includes("/pipelines/reorder"))?.body).toEqual({
        kind: "embed",
        order: ["p2", "p1"],
      }),
    );
  });

  it("turns a pipeline off with a PATCH and deletes only after asking", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen, [REMOTE]));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Remote embeddings");

    const toggle = screen.getByRole("switch", { name: "Pipeline Remote embeddings on or off" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    await user.click(toggle);
    await waitFor(() =>
      expect(seen.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: false }),
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(seen.some((c) => c.method === "DELETE")).toBe(false);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(seen.some((c) => c.method === "DELETE")).toBe(true));
  });

  it("opens one 'How it works' overlay with the model in five points", async () => {
    vi.stubGlobal("fetch", api([], []));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "How it works" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Whole files leave whole/)).toBeInTheDocument();
    expect(within(dialog).getByText(/A provider is a provider/)).toBeInTheDocument();
  });
});
