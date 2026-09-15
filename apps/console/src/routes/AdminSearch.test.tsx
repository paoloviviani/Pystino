import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
 * server refusal arrives as its sentence rather than a code.
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
    is_default: false,
  },
  {
    name: "exa",
    label: "Exa (web search)",
    description: "Exa",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.exa.ai",
    is_default: false,
  },
];

interface PolicyCalls {
  set: { groupId: string; body: unknown }[];
}

function searchRoutes(
  calls: PolicyCalls = { set: [] },
  options: {
    failPolicyWith?: { status: number; message: string };
    cereaBackend?: string | null;
  } = {},
) {
  const { failPolicyWith, cereaBackend = "linkup" } = options;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url.includes("/api/admin/provider-plugins"))
      return new Response(JSON.stringify(PLUGINS), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    if (url.includes("/api/admin/providers")) {
      return jsonResponse({
        items: [backend(), backend({ id: "p2", name: "exa", base_url: "https://api.exa.ai" })],
        total: 2,
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
