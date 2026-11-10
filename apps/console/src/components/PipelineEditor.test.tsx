import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPipeline, MyPipeline } from "./PipelineEditor";

/** The pipeline editor: one form for an administrator and a user; a rule is one sentence "When policies + conditions -> mode". */

const OPTIONS = {
  users: [{ id: "u1", name: "ana@example.org" }],
  groups: [{ id: "g1", name: "Finance" }],
  keys: [],
  providers: [{ id: "pv1", name: "OpenAI" }],
  models: [{ id: "m1", name: "text-embedding-3-small", provider_id: "pv1", provider: "OpenAI", kind: "embedding" }],
  readers: [
    { id: "markitdown", name: "markitdown", local: true },
    { id: "docling", name: "docling", local: true },
    { id: "mistral", name: "mistral", local: false },
  ],
  policies: [{ id: "pol1", name: "Default" }],
  own_policies: [{ id: "own1", name: "Mine" }],
  classifiers: [{ id: "cl1", name: "Unannounced project" }],
  entity_types: null,
};

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function api(seen: Seen[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/options")) return jsonResponse(OPTIONS);
    if (url.includes("/pipelines/check")) return jsonResponse({ error: null, issues: [], soft_cap: 20 });
    if (url.includes("/pipelines") && method === "GET") {
      return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
    }
    if (url.includes("/pipelines")) return jsonResponse({ id: "new" });
    return jsonResponse({});
  });
}

function renderEditor(element: React.ReactElement, path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin/privacy/pipelines/:id" element={element} />
          <Route path="/privacy/pipelines/:id" element={element} />
          <Route path="/admin/privacy" element={<p>list</p>} />
          <Route path="/privacy" element={<p>mine</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

const POST = (seen: Seen[]) => seen.find((c) => c.method === "POST" && !c.url.includes("check"));
const posted = (seen: Seen[]) => waitFor(() => expect(POST(seen)).toBeDefined());

function pipelineJson(over: Record<string, unknown> = {}) {
  return {
    id: "p1",
    owner_id: null,
    kind: "content",
    name: "Embedding",
    enabled: true,
    position: 0,
    who: { mode: "all", ids: [] },
    target: { mode: "all", providers: [], models: [], model_types: [] },
    rules: [],
    labels: {},
    summary: [],
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
    ...over,
  };
}

function apiWith(seen: Seen[], items: unknown[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/options")) return jsonResponse(OPTIONS);
    if (url.includes("/pipelines/check")) return jsonResponse({ error: null, issues: [], soft_cap: 20 });
    if (url.includes("/pipelines") && method === "GET") {
      return jsonResponse({ items, total: items.length, limit: 200, offset: 0 });
    }
    return jsonResponse(items[0] ?? { id: "new" });
  });
}

describe("Pipeline editor", () => {
  it("builds a content pipeline with a blocking rule and saves exactly that", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");

    await user.type(await screen.findByLabelText("Name"), "Remote providers");
    await user.click(screen.getByRole("button", { name: "Embedding" }));
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add policy..."), "pol1");
    await user.selectOptions(screen.getByLabelText("Add a condition"), "finds");
    await user.clear(screen.getByLabelText("Number of items"));
    await user.type(screen.getByLabelText("Number of items"), "3");
    await user.selectOptions(screen.getByRole("combobox", { name: "Then" }), "block");
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));

    await posted(seen);
    expect(POST(seen)?.url).toBe("/api/admin/privacy/pipelines");
    expect(POST(seen)?.body).toMatchObject({
      kind: "content",
      name: "Remote providers",
      enabled: true,
      who: { mode: "all", ids: [] },
      target: { mode: "all", providers: [], models: [], model_types: ["embedding"] },
      rules: [
        {
          name: "Rule 1",
          enabled: true,
          policies: ["pol1"],
          match: "all",
          mode: "block",
          overrides: {},
          conditions: [{ key: "finds", values: [], negate: false, count: 3 }],
        },
      ],
    });
    expect(POST(seen)?.body).not.toHaveProperty("otherwise");
    expect(JSON.stringify(POST(seen)?.body)).not.toMatch(/observe_only|redact_with/);
  });

  it("says once, above the rules, how they apply, and has no pass or refuse wording", async () => {
    vi.stubGlobal("fetch", api([]));
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await screen.findByLabelText("Name");
    expect(
      screen.getByText(
        "Rules apply top to bottom. A rule that blocks refuses the request; otherwise every matching rule hides what it finds, and the first rule to hide a piece of text decides how.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/everything else|first match|observe/i)).toBeNull();
  });

  it("offers who and target scopes to an administrator, with 'all except' over providers", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await screen.findByLabelText("Name");

    await user.click(screen.getByRole("button", { name: "Billing groups" }));
    await user.click(screen.getByRole("button", { name: "Finance" }));
    await user.click(screen.getByRole("button", { name: "All except" }));
    await user.click(screen.getByRole("button", { name: "OpenAI" }));
    await user.type(screen.getByLabelText("Name"), "P");
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));

    await posted(seen);
    expect(POST(seen)?.body).toMatchObject({
      who: { mode: "groups", ids: ["g1"] },
      target: { mode: "except", providers: ["pv1"], models: [] },
    });
  });

  it("keeps per-type overrides collapsed and saves them with the rule", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await user.type(await screen.findByLabelText("Name"), "O");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add policy..."), "pol1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Then" }), "anonymise_restore");
    await user.type(screen.getByLabelText("Entity type"), "it vat code");
    await user.click(screen.getByRole("button", { name: "Add override" }));
    await user.selectOptions(screen.getByLabelText("Mode for IT_VAT_CODE"), "redact");
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));
    await posted(seen);
    expect(POST(seen)?.body).toMatchObject({
      rules: [{ mode: "anonymise_restore", overrides: { IT_VAT_CODE: "redact" } }],
    });
  });

  it("shows the extractors for 'extract with', and hiding on the extracted text", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=extract");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    await user.click(screen.getByRole("radio", { name: /Extract with/ }));

    const readers = screen.getByRole("group", { name: "Reader" });
    expect(within(readers).getByRole("button", { name: "markitdown" })).toBeInTheDocument();
    expect(within(readers).getByRole("button", { name: "docling" })).toBeInTheDocument();
    expect(within(readers).getByRole("button", { name: "mistral" })).toBeInTheDocument();
    expect(screen.getByText("Hide in the extracted text (optional)")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Block/ })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Refuse|Pass/ })).toBeNull();
  });

  it("has no extract action on a content rule: its mode says what it does", async () => {
    vi.stubGlobal("fetch", api([]));
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await screen.findByLabelText("Name");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    expect(screen.queryByRole("radio", { name: /Extract with/ })).toBeNull();
    expect(screen.getByRole("combobox", { name: "Then" })).toBeInTheDocument();
  });

  it("warns that a file-name-only protection is defeated by renaming", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=extract");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add a condition"), "filename");
    await user.click(screen.getByRole("radio", { name: /Block/ }));
    expect(screen.getByText("A renamed file will not be caught")).toBeInTheDocument();
  });

  it("offers exactly the conditions of the kind, and chooses between all and any", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=extract");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const add = screen.getByLabelText("Add a condition");
    expect(within(add).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "+ Add condition",
      "File type",
      "Is a scan",
      "File name",
    ]);
    await user.selectOptions(add, "scan");
    await user.selectOptions(screen.getByLabelText("Add a condition"), "filename");
    const match = screen.getByRole("group", { name: "Conditions match" });
    await user.click(within(match).getByRole("button", { name: "Any" }));
    expect(within(match).getByRole("button", { name: "Any" })).toHaveAttribute("aria-pressed", "true");
  });

  it("offers the content conditions on a content pipeline", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const add = screen.getByLabelText("Add a condition");
    expect(within(add).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "+ Add condition",
      "finds at least",
      "Classified as",
    ]);
  });

  it("builds a classified-as condition and says the text goes to the model's provider", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=content");
    await user.type(await screen.findByLabelText("Name"), "Classed");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add policy..."), "pol1");
    await user.selectOptions(screen.getByLabelText("Add a condition"), "classified");
    await user.selectOptions(screen.getByLabelText("Classifier"), "cl1");
    expect(
      screen.getByRole("button", { name: "Classifying sends the text to the model's provider." }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Flip Classified as" }));
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));
    await posted(seen);
    expect(POST(seen)?.body).toMatchObject({
      rules: [{ conditions: [{ key: "classified", values: ["cl1"], negate: true }] }],
    });
  });

  it("offers the user's own policies next to the shared ones, and never classified", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<MyPipeline />, "/privacy/pipelines/new?kind=content");
    expect(await screen.findByText("Only me")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Billing groups" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    expect(within(screen.getByLabelText("Add a condition")).queryByRole("option", { name: "Classified as" })).toBeNull();
    const policy = screen.getByLabelText("Add policy...");
    expect(within(policy).getByRole("option", { name: "Default" })).toBeInTheDocument();
    expect(within(policy).getByRole("option", { name: "Mine" })).toBeInTheDocument();
  });

  it("shows a migration note as a warning on the rule and sends it back unchanged", async () => {
    const seen: Seen[] = [];
    const note = "Migration 0086: it needs a policy to look for.";
    vi.stubGlobal(
      "fetch",
      apiWith(seen, [
        pipelineJson({
          name: "Migrated",
          rules: [
            {
              id: "r1",
              name: "Old rule",
              enabled: false,
              note,
              policies: ["pol1"],
              match: "all",
              conditions: [],
              mode: "block",
              overrides: {},
              action: null,
              issues: [],
            },
          ],
        }),
      ]),
    );
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/p1");
    expect(await screen.findAllByText(note)).not.toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen.some((c) => c.method === "PATCH")).toBe(true));
    expect(seen.find((c) => c.method === "PATCH")?.body).toMatchObject({ rules: [{ note }] });
  });

  it.each([
    ["admin", <AdminPipeline key="a" />, "/admin/privacy/pipelines/p1", "/api/admin/privacy/pipelines/p1"],
    ["user", <MyPipeline key="u" />, "/privacy/pipelines/p1", "/api/me/privacy/pipelines/p1"],
  ])("saves an existing %s pipeline without the kind, which is fixed once created", async (_who, element, path, url) => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", apiWith(seen, [pipelineJson()]));
    const user = userEvent.setup();
    renderEditor(element, path);
    await user.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(seen.some((c) => c.method === "PATCH")).toBe(true));
    const patch = seen.find((c) => c.method === "PATCH");
    expect(patch?.url).toBe(url);
    expect(patch?.body).toMatchObject({ name: "Embedding", enabled: true });
    expect(patch?.body).not.toHaveProperty("kind");
  });
});
