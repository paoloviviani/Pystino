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
  groups: [{ id: "g1", name: "Clinic" }],
  keys: [],
  providers: [{ id: "pv1", name: "OpenAI" }],
  models: [{ id: "m1", name: "text-embedding-3-small", provider_id: "pv1", provider: "OpenAI", kind: "embedding" }],
  readers: [
    { id: "builtin", name: "Built-in extractor", builtin: true },
    { id: "mistral", name: "mistral", builtin: false },
  ],
  policies: [{ id: "pol1", name: "Default" }],
  own_policies: [{ id: "own1", name: "Mine" }],
  profiles: [{ id: "prof1", name: "Medical" }],
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
    if (url.includes("/profiles")) return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
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
    await user.selectOptions(screen.getByLabelText("Add a condition"), "health");
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
          conditions: [{ key: "health", values: [], negate: false, decide: "patterns" }],
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

    await user.click(screen.getByRole("button", { name: "Groups" }));
    await user.click(screen.getByRole("button", { name: "Clinic" }));
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
    expect(within(readers).getByRole("button", { name: "the built-in extractor" })).toBeInTheDocument();
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

  it("fixes a user's who to themselves and does not offer 'nothing' as a rule's action", async () => {
    vi.stubGlobal("fetch", api([]));
    const user = userEvent.setup();
    renderEditor(<MyPipeline />, "/privacy/pipelines/new?kind=embed");

    expect(await screen.findByText("Only me")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Groups" })).toBeNull();
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
