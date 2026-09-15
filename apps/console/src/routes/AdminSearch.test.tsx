import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminGroup, AdminModel, AdminProvider, ProviderPlugin } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminSearch } from "./AdminSearch";

/**
 * The per-group search policy card: one backend per group, chosen only among
 * the backends the group is granted. What the tests hold is the security
 * property the screen exists for — an ungranted backend is never offered —
 * plus that the choice is sent to the policy endpoint verbatim, and that a
 * server refusal arrives as its sentence rather than a code. The backend
 * dialog is held here too: where a vendor documents more than one host, the
 * form offers exactly those as a choice and never invents a URL, and an
 * endpoint the vendor does not document is kept rather than silently moved.
 */

function backend(overrides: Partial<AdminProvider> = {}): AdminProvider {
  return {
    id: "p1",
    name: "linkup",
    description: null,
    base_url: "https://api.linkup.so/v1",
    api_key_hint: "luk_…1",
    has_api_key: true,
    extra_headers: {},
    is_active: true,
    prefix: "",
    plugin: "linkup",
    kind: "search",
    plugin_kind: "search",
    billing_mode: "own_prices",
    unpriced_model_count: 0,
    model_count: 1,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

function tier(overrides: Partial<AdminModel> = {}): AdminModel {
  return {
    id: "m-linkup",
    name: "linkup",
    upstream_model: "search",
    provider_id: "p1",
    provider_name: "linkup",
    provider_is_active: true,
    provider_kind: "search",
    kind: "search",
    display_name: null,
    description: null,
    is_active: true,
    is_public: false,
    context_window: null,
    max_output_tokens: null,
    max_input_tokens: null,
    input_modalities: [],
    output_modalities: [],
    supported_features: [],
    created_at: "2026-09-01T00:00:00Z",
    current_price: null,
    granted_to: ["cerea"],
    granted_to_users: [],
    ...overrides,
  };
}

function group(overrides: Partial<AdminGroup> = {}): AdminGroup {
  return {
    id: "g-cerea",
    name: "cerea",
    description: null,
    source: "manual",
    is_active: true,
    member_count: 3,
    models: ["linkup"],
    search_backend: "linkup",
    ...overrides,
  };
}

const PLUGINS: ProviderPlugin[] = [
  {
    name: "linkup",
    label: "Linkup (web search)",
    description: "Linkup",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.linkup.so/v1",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "exa",
    label: "Exa (web search)",
    description: "Exa",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.exa.ai",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "jina",
    label: "Jina (web search)",
    description: "Jina",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://s.jina.ai",
    base_url_options: [
      { url: "https://s.jina.ai", label: "s.jina.ai — global (default)" },
      { url: "https://eu.s.jina.ai", label: "eu.s.jina.ai — all processing stays in the EU" },
    ],
    is_default: false,
  },
];

interface PolicyCalls {
  set: { groupId: string; body: unknown }[];
}

interface Writes {
  captured: { url: string; method: string; body: unknown }[];
}

function searchRoutes(
  calls: PolicyCalls = { set: [] },
  options: {
    failPolicyWith?: { status: number; message: string };
    cereaBackend?: string | null;
    writes?: Writes;
    providers?: AdminProvider[];
  } = {},
) {
  const { failPolicyWith, cereaBackend = "linkup", writes, providers } = options;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url.includes("/api/admin/provider-plugins"))
      return new Response(JSON.stringify(PLUGINS), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    if (url.includes("/api/admin/providers")) {
      if (method === "POST" || method === "PATCH") {
        writes?.captured.push({ url, method, body: JSON.parse(String(init?.body)) });
        return jsonResponse({ id: "p-new" });
      }
      return jsonResponse({
        items: providers ?? [
          backend(),
          backend({ id: "p2", name: "exa", base_url: "https://api.exa.ai" }),
        ],
        total: providers?.length ?? 2,
        limit: 200,
        offset: 0,
      });
    }
    if (url.includes("/api/admin/models")) {
      return jsonResponse({
        items: [
          tier(),
          tier({ id: "m-exa", name: "exa", provider_id: "p2", provider_name: "exa", granted_to: [] }),
        ],
        total: 2,
        limit: 200,
        offset: 0,
      });
    }
    if (url.includes("/api/admin/groups/") && url.endsWith("/search-backend") && method === "PUT") {
      const body = JSON.parse(String(init?.body));
      if (failPolicyWith) {
        return new Response(
          JSON.stringify({ error: { message: failPolicyWith.message } }),
          { status: failPolicyWith.status },
        );
      }
      calls.set.push({ groupId: url.split("/").at(-2) ?? "", body });
      return new Response(null, { status: 204 });
    }
    if (url.includes("/api/admin/groups")) {
      return jsonResponse({
        items: [
          group({ search_backend: cereaBackend }),
          group({ id: "g-other", name: "other", models: [], search_backend: null }),
        ],
        total: 2,
        limit: 200,
        offset: 0,
      });
    }
    return jsonResponse([]);
  });
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AdminSearch group policy", () => {
  it("offers only granted backends, with the policy preselected", async () => {
    vi.stubGlobal("fetch", searchRoutes());
    renderScreen(<AdminSearch />);

    const select = await screen.findByLabelText("Search backend for cerea");
    const texts = [...(select as unknown as HTMLSelectElement).options].map((o) => o.text);
    // exa is a backend but not granted to cerea: never offered.
    expect(texts).toEqual(["Unset — no unified search", "linkup"]);
    expect((select as unknown as HTMLSelectElement).value).toBe("m-linkup");

    const other = await screen.findByLabelText("Search backend for other");
    expect([...(other as unknown as HTMLSelectElement).options].map((o) => o.text)).toEqual([
      "Unset — no unified search",
    ]);
  });

  it("sends the chosen backend id to the policy endpoint", async () => {
    const calls: PolicyCalls = { set: [] };
    vi.stubGlobal("fetch", searchRoutes(calls));
    const user = userEvent.setup();
    renderScreen(<AdminSearch />);

    const select = await screen.findByLabelText("Search backend for cerea");
    await user.selectOptions(select, "m-linkup");

    await waitFor(() =>
      expect(calls.set).toEqual([{ groupId: "g-cerea", body: { model_id: "m-linkup" } }]),
    );
  });

  it("clearing the select clears the policy", async () => {
    const calls: PolicyCalls = { set: [] };
    vi.stubGlobal("fetch", searchRoutes(calls));
    const user = userEvent.setup();
    renderScreen(<AdminSearch />);

    const select = await screen.findByLabelText("Search backend for cerea");
    await user.selectOptions(select, "");

    await waitFor(() =>
      expect(calls.set).toEqual([{ groupId: "g-cerea", body: { model_id: null } }]),
    );
  });

  it("shows a server refusal as its sentence", async () => {
    vi.stubGlobal(
      "fetch",
      searchRoutes(
        { set: [] },
        { cereaBackend: null, failPolicyWith: { status: 409, message: "Group 'cerea' is not granted 'linkup'." } },
      ),
    );
    const user = userEvent.setup();
    renderScreen(<AdminSearch />);

    const select = await screen.findByLabelText("Search backend for cerea");
    await user.selectOptions(select, "m-linkup");

    await screen.findByText("Group 'cerea' is not granted 'linkup'.");
  });
});

describe("AdminSearch backend dialog", () => {
  it("offers a vendor's documented hosts as a choice, default first", async () => {
    // The constrained form of the choice: two documented values, labelled —
    // not a free-text URL field that invites a typo no listing would catch.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", searchRoutes());
    renderScreen(<AdminSearch />);

    await user.click(await screen.findByRole("button", { name: "Add backend" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Vendor"), "jina");

    const endpoint = dialog.getByLabelText("Endpoint") as HTMLSelectElement;
    const texts = [...endpoint.options].map((option) => option.text);
    expect(texts).toEqual([
      "s.jina.ai — global (default)",
      "eu.s.jina.ai — all processing stays in the EU",
    ]);
    expect(endpoint.value).toBe("https://s.jina.ai");
  });

  it("sends the chosen documented host on create", async () => {
    const user = userEvent.setup({ delay: null });
    const writes: Writes = { captured: [] };
    vi.stubGlobal("fetch", searchRoutes({ set: [] }, { writes }));
    renderScreen(<AdminSearch />);

    await user.click(await screen.findByRole("button", { name: "Add backend" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Vendor"), "jina");
    await user.type(dialog.getByLabelText("API key"), "jina-key-1");
    await user.selectOptions(dialog.getByLabelText("Endpoint"), "https://eu.s.jina.ai");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(writes.captured.length).toBeGreaterThan(0));
    const body = writes.captured[0]?.body as Record<string, unknown>;
    expect(body.base_url).toBe("https://eu.s.jina.ai");
    expect(body.plugin).toBe("jina");
    expect(body.kind).toBe("search");
  });

  it("offers no endpoint choice where the vendor documents one host", async () => {
    const user = userEvent.setup({ delay: null });
    const writes: Writes = { captured: [] };
    vi.stubGlobal("fetch", searchRoutes({ set: [] }, { writes }));
    renderScreen(<AdminSearch />);

    await user.click(await screen.findByRole("button", { name: "Add backend" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Vendor"), "linkup");
    await user.type(dialog.getByLabelText("API key"), "luk-key-1");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    expect(dialog.queryByLabelText("Endpoint")).not.toBeInTheDocument();
    await waitFor(() => expect(writes.captured.length).toBeGreaterThan(0));
    const body = writes.captured[0]?.body as Record<string, unknown>;
    // No URL sent: the plugin's default is the whole answer, and sending
    // nothing is what applies it.
    expect("base_url" in body).toBe(false);
  });

  it("does not carry the previous vendor's URL to the next one", async () => {
    // Pick Jina, then switch to Linkup: the endpoint field empties, because a
    // Jina host stored on a Linkup backend would be a configuration that
    // looks right and searches the wrong vendor.
    const user = userEvent.setup({ delay: null });
    const writes: Writes = { captured: [] };
    vi.stubGlobal("fetch", searchRoutes({ set: [] }, { writes }));
    renderScreen(<AdminSearch />);

    await user.click(await screen.findByRole("button", { name: "Add backend" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Vendor"), "jina");
    await user.selectOptions(dialog.getByLabelText("Vendor"), "linkup");
    await user.type(dialog.getByLabelText("API key"), "luk-key-1");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    expect(dialog.queryByLabelText("Endpoint")).not.toBeInTheDocument();
    await waitFor(() => expect(writes.captured.length).toBeGreaterThan(0));
    expect("base_url" in (writes.captured[0]?.body as Record<string, unknown>)).toBe(false);
  });

  it("editing preselects the stored host and sends the switch", async () => {
    const user = userEvent.setup({ delay: null });
    const writes: Writes = { captured: [] };
    vi.stubGlobal(
      "fetch",
      searchRoutes(
        { set: [] },
        {
          writes,
          providers: [backend({ name: "jina", plugin: "jina", base_url: "https://s.jina.ai" })],
        },
      ),
    );
    renderScreen(<AdminSearch />);

    await waitFor(() => expect(screen.getByText("jina")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    const endpoint = dialog.getByLabelText("Endpoint") as HTMLSelectElement;
    expect(endpoint.value).toBe("https://s.jina.ai");

    await user.selectOptions(endpoint, "https://eu.s.jina.ai");
    await user.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes.captured.length).toBeGreaterThan(0));
    expect(writes.captured[0]?.method).toBe("PATCH");
    expect((writes.captured[0]?.body as Record<string, unknown>).base_url).toBe(
      "https://eu.s.jina.ai",
    );
  });

  it("keeps an endpoint the vendor does not document rather than moving it", async () => {
    // A URL the operator set by hand — a proxy — is not the plugin's choice
    // to overwrite: it stays on the screen, preselected, and an edit that
    // changes nothing stays unsavable, so no unrelated save can silently
    // move the host either way.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      searchRoutes(
        { set: [] },
        {
          providers: [
            backend({
              name: "jina",
              plugin: "jina",
              base_url: "https://jina.internal.test",
            }),
          ],
        },
      ),
    );
    renderScreen(<AdminSearch />);

    await waitFor(() => expect(screen.getByText("jina")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    const endpoint = dialog.getByLabelText("Endpoint") as HTMLSelectElement;
    expect(endpoint.value).toBe("https://jina.internal.test");
    expect(screen.getByRole("option", { name: "https://jina.internal.test" })).toBeTruthy();

    const save = dialog.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.selectOptions(endpoint, "https://eu.s.jina.ai");
    expect(save).toBeEnabled();
    await user.selectOptions(endpoint, "https://jina.internal.test");
    expect(save).toBeDisabled();
  });
});
