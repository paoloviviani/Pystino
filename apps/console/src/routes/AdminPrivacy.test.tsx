import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrivacyPipeline } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacy } from "./AdminPrivacy";

/** The Pipelines tab: pipelines by kind, each with its scope and its rules as sentences. */

function rule(over: Record<string, unknown>) {
  return {
    enabled: true,
    note: null,
    policies: [],
    match: "all",
    conditions: [],
    mode: "off",
    overrides: {},
    action: null,
    issues: [],
    ...over,
  };
}

function pipeline(overrides: Partial<PrivacyPipeline> & { id: string; name: string }): PrivacyPipeline {
  return {
    owner_id: null,
    kind: "content",
    enabled: true,
    position: 0,
    who: { mode: "all", ids: [] },
    target: { mode: "all", providers: [], models: [], model_types: [] },
    rules: [],
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
  target: { mode: "only", providers: ["pv1"], models: [], model_types: ["embedding", "rerank"] },
  labels: { g1: "Finance", pv1: "OpenAI", pol1: "Default", pol2: "Names" },
  rules: [
    rule({
      id: "r1",
      name: "Names found",
      policies: ["pol2"],
      mode: "block",
      conditions: [{ key: "finds", values: [], negate: false, count: 2 }],
    }),
    rule({ id: "r2", name: "Hide the rest", policies: ["pol1"], mode: "anonymise_restore" }),
  ],
} as never);
const SECOND = pipeline({ id: "p2", name: "Second", position: 1 });
const EXTRACT = pipeline({
  id: "p3",
  kind: "extract",
  name: "Reports",
  rules: [
    rule({
      id: "r3",
      name: "Scans",
      conditions: [{ key: "scan", values: [], negate: false, count: 1 }],
      action: { level: "extract_with", reader: "docling" },
      issues: [{ code: "unavailable_now", message: "docling is not offered", rule_id: null, rule_name: null }],
    }),
  ],
} as never);

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
    vi.stubGlobal("fetch", api([], [REMOTE, SECOND, EXTRACT]));
    renderPage();

    const content = await screen.findByRole("region", { name: "Content" });
    expect(within(content).getByText("Remote embeddings")).toBeInTheDocument();
    expect(within(content).getByText("Billing groups: Finance")).toBeInTheDocument();
    expect(within(content).getByText("Only OpenAI · Embedding + Rerank")).toBeInTheDocument();
    expect(
      within(content).getByText("When Names + it finds at least 2 items → block"),
    ).toBeInTheDocument();
    expect(within(content).getByText("When Default → anonymise, restore in the answer")).toBeInTheDocument();
    expect(within(content).queryByText(/everything else|pass|refuse/i)).toBeNull();

    const extract = screen.getByRole("region", { name: "Extract" });
    expect(
      within(extract).getByText(/When the file is a scan → extract with docling/),
    ).toBeInTheDocument();
    // A rule with a warning is flagged on its pipeline.
    expect(within(extract).getByText("Check")).toBeInTheDocument();
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
          policies: ["pol1"],
          match: "all",
          mode: "block",
          overrides: {},
          action: null,
          note: "Switched off by the migration: it used a check that no longer exists.",
          conditions: [],
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
        kind: "content",
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
    expect(within(dialog).getByText(/first rule to hide a piece of text/)).toBeInTheDocument();
  });
});
