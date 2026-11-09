import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPipeline, MyPipeline } from "./PipelineEditor";

/** The pipeline editor: one form for an administrator and a user, with the ladder as radios. */

const OPTIONS = {
  users: [{ id: "u1", name: "ana@example.org" }],
  groups: [{ id: "g1", name: "Finance" }],
  keys: [],
  providers: [{ id: "pv1", name: "OpenAI" }],
  models: [{ id: "m1", name: "text-embedding-3-small", provider_id: "pv1", provider: "OpenAI", kind: "embedding" }],
  readers: [
    { id: "builtin", name: "Built-in extractor", builtin: true },
    { id: "mistral", name: "mistral", builtin: false },
  ],
  policies: [{ id: "pol1", name: "Default" }],
  own_policies: [{ id: "own1", name: "Mine" }],
  classifiers: [{ id: "cl1", name: "Unannounced project" }],
  entity_types: null,
  engines: ["markitdown", "ocr", "docling"],
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

describe("Pipeline editor", () => {
  it("builds an embedding pipeline with a refusing rule and saves exactly that", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=embed");

    await user.type(await screen.findByLabelText("Name"), "Remote embeddings");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add a condition"), "finds");
    await user.selectOptions(screen.getByLabelText("Policy"), "pol1");
    await user.clear(screen.getByLabelText("Number of items"));
    await user.type(screen.getByLabelText("Number of items"), "3");
    await user.click(screen.getByRole("radio", { name: /Refuse/ }));
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));

    await waitFor(() => expect(seen.some((c) => c.method === "POST" && !c.url.includes("check"))).toBe(true));
    const post = seen.find((c) => c.method === "POST" && !c.url.includes("check"));
    expect(post?.url).toBe("/api/admin/privacy/pipelines");
    expect(post?.body).toMatchObject({
      kind: "embed",
      name: "Remote embeddings",
      enabled: true,
      who: { mode: "all", ids: [] },
      target: { mode: "all", providers: [], models: [] },
      rules: [
        {
          name: "Rule 1",
          enabled: true,
          observe_only: false,
          conditions: [{ key: "finds", values: ["pol1"], negate: false, count: 3 }],
          action: { level: "refuse" },
        },
      ],
      otherwise: { level: "pass" },
    });
  });

  it("offers who and target scopes to an administrator, with 'all except' over providers", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=embed");
    await screen.findByLabelText("Name");

    await user.click(screen.getByRole("button", { name: "Billing groups" }));
    await user.click(screen.getByRole("button", { name: "Finance" }));
    await user.click(screen.getByRole("button", { name: "All except" }));
    await user.click(screen.getByRole("button", { name: "OpenAI" }));
    await user.type(screen.getByLabelText("Name"), "P");
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));

    await waitFor(() => expect(seen.some((c) => c.method === "POST" && !c.url.includes("check"))).toBe(true));
    expect(seen.find((c) => c.method === "POST" && !c.url.includes("check"))?.body).toMatchObject({
      who: { mode: "groups", ids: ["g1"] },
      target: { mode: "except", providers: ["pv1"], models: [] },
    });
  });

  it("shows readers and the engine for 'read with', and a redact-with policy for read", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=read");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("radio", { name: /Read with/ }));

    const readers = screen.getByRole("group", { name: "Reader" });
    expect(within(readers).getByRole("button", { name: "Built-in extractor" })).toBeInTheDocument();
    expect(within(readers).getByRole("button", { name: "mistral" })).toBeInTheDocument();
    expect(screen.getByLabelText("Engine")).toBeInTheDocument();
    expect(screen.getByLabelText("Redact with (on the returned text)")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Refuse/ }));
    expect(screen.queryByLabelText("Redact with (on the returned text)")).toBeNull();
    expect(screen.getByText("Refused requests are not handled further, so no policy applies.")).toBeInTheDocument();
  });

  it("says embedding and chat never reroute: the provider stays the caller's choice", async () => {
    vi.stubGlobal("fetch", api([]));
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=chat");
    await screen.findByLabelText("Name");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("The provider stays the caller's choice")).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Read with/ })).toBeNull();
  });

  it("warns that a file-name-only protection is defeated by renaming", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=read");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add a condition"), "filename");
    await user.click(screen.getByRole("radio", { name: /Refuse/ }));
    expect(screen.getByText("A renamed file will not be caught")).toBeInTheDocument();
  });

  it("offers exactly the conditions of the kind, and none of health, ids or language", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=read");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const add = screen.getByLabelText("Add a condition");
    const names = within(add).getAllByRole("option").map((o) => o.textContent);
    // Reading decides from the file: only its facts, no content condition.
    expect(names).toEqual(["+ Add condition", "File type", "Is a scan", "File name"]);
    expect(screen.queryByText(/health|language/i)).toBeNull();
    // The hint says why, in a (?) that opens on a click.
    await user.click(
      screen.getByRole("button", {
        name: "Reading rules decide from the file, not its content; content is checked when the text is embedded or sent to chat.",
      }),
    );
  });

  it("offers the content conditions for embed and chat", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=embed");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const add = screen.getByLabelText("Add a condition");
    const names = within(add).getAllByRole("option").map((o) => o.textContent);
    expect(names).toEqual(["+ Add condition", "Policy finds", "Classified as"]);
    expect(screen.queryByRole("button", { name: /Reading rules decide/ })).toBeNull();
  });

  it("builds a classified-as condition and says the text goes to the model's provider", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=chat");
    await user.type(await screen.findByLabelText("Name"), "Classed");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add a condition"), "classified");
    await user.selectOptions(screen.getByLabelText("Classifier"), "cl1");
    expect(
      screen.getByRole("button", { name: "Classifying sends the text to the model's provider." }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Flip Classified as" }));
    await user.click(screen.getByRole("radio", { name: /Refuse/ }));
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(seen.some((c) => c.method === "POST" && !c.url.includes("check"))).toBe(true));
    expect(seen.find((c) => c.method === "POST" && !c.url.includes("check"))?.body).toMatchObject({
      rules: [{ conditions: [{ key: "classified", values: ["cl1"], negate: true }] }],
    });
  });

  it("says there are no classifiers when there are none", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/options")) return jsonResponse({ ...OPTIONS, classifiers: [] });
        if (url.includes("/pipelines/check")) return jsonResponse({ error: null, issues: [], soft_cap: 20 });
        return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
      }),
    );
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/new?kind=chat");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    await user.selectOptions(screen.getByLabelText("Add a condition"), "classified");
    expect(screen.getByText(/No classifiers yet/)).toBeInTheDocument();
  });

  it("offers the user's own policies next to the shared ones in 'finds', and never classified", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<MyPipeline />, "/privacy/pipelines/new?kind=chat");
    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const add = screen.getByLabelText("Add a condition");
    expect(within(add).queryByRole("option", { name: "Classified as" })).toBeNull();
    await user.selectOptions(add, "finds");
    const policy = screen.getByLabelText("Policy");
    expect(within(policy).getByRole("option", { name: "Default" })).toBeInTheDocument();
    expect(within(policy).getByRole("option", { name: "Mine" })).toBeInTheDocument();
  });

  it("shows a migration note as a warning on the rule and sends it back unchanged", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
        if (url.endsWith("/options")) return jsonResponse(OPTIONS);
        if (url.includes("/pipelines/check")) return jsonResponse({ error: null, issues: [], soft_cap: 20 });
        if (url.includes("/pipelines") && method === "GET") {
          return jsonResponse({
            items: [
              {
                id: "p1",
                owner_id: null,
                kind: "chat",
                name: "Migrated",
                enabled: true,
                position: 0,
                who: { mode: "all", ids: [] },
                target: { mode: "all", providers: [], models: [] },
                rules: [
                  {
                    id: "r1",
                    name: "Old rule",
                    enabled: false,
                    observe_only: false,
                    note: "Switched off: it used a check that no longer exists.",
                    conditions: [],
                    action: { level: "refuse" },
                    issues: [],
                  },
                ],
                otherwise: { id: "o", name: "else", enabled: true, observe_only: false, note: null, conditions: [], action: { level: "pass" }, issues: [] },
                labels: {},
                summary: [],
                created_at: "",
                updated_at: "",
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          });
        }
        return jsonResponse({ id: "p1" });
      }),
    );
    const user = userEvent.setup();
    renderEditor(<AdminPipeline />, "/admin/privacy/pipelines/p1");
    expect(
      await screen.findByText("Switched off: it used a check that no longer exists."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen.some((c) => c.method === "PATCH")).toBe(true));
    expect(seen.find((c) => c.method === "PATCH")?.body).toMatchObject({
      rules: [{ note: "Switched off: it used a check that no longer exists." }],
    });
  });

  it("fixes a user's who to themselves and does not offer 'nothing' as a rule's action", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<MyPipeline />, "/privacy/pipelines/new?kind=embed");

    expect(await screen.findByText("Only me")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Billing groups" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    const pass = screen.getAllByRole("radio", { name: /Pass/ })[0] as HTMLInputElement;
    // What happens anyway is not an addition: it cannot be picked once something else is.
    await user.click(screen.getAllByRole("radio", { name: /Refuse/ })[0] as HTMLElement);
    expect(pass).toBeDisabled();
    // "Everything else" may add a policy: the user's own are offered next to the administrator's.
    await user.click(screen.getAllByRole("button", { name: "Edit" }).at(-1) as HTMLElement);
    const policy = screen.getByLabelText("Redact with (on the input)");
    expect(within(policy).getByRole("option", { name: "Default" })).toBeInTheDocument();
    expect(within(policy).getByRole("option", { name: "Mine" })).toBeInTheDocument();
  });
});
