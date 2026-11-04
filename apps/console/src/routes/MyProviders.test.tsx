import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Me } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { MyProviders } from "./MyProviders";

/**
 * Bring your own key: a user's own providers and models.
 *
 * These are the administrators' screens pointed at `/api/me/...`, so the
 * properties worth pinning are the ones that make them safe and clear for a
 * user: the page exists only with the capability, every call stays on the
 * user's own endpoints, the key is write-only, and the gateway's guard errors
 * arrive as an explanation rather than a status line.
 */

function me(byok: boolean): Me {
  return {
    id: "u1",
    email: "alice@example.org",
    display_name: "Alice",
    is_admin: false,
    groups: [],
    default_billing_group: null,
    issuer: "local",
    has_password: true,
    capabilities: { code_agents: false, api_keys: true, local_connectors: false, byok, console: true },
    chat_enabled: false,
    chat_url: "",
  } as Me;
}

const PLUGINS = [
  {
    name: "generic",
    label: "OpenAI-compatible",
    description: "Forwards requests unchanged.",
    kind: "provider",
    billing_modes: ["own_prices"],
    default_base_url: null,
    base_url_options: [],
    is_default: true,
    protocol: "openai",
  },
  {
    name: "anthropic",
    label: "Anthropic (direct)",
    description: "Anthropic's own API.",
    kind: "provider",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.anthropic.com/v1",
    base_url_options: [],
    is_default: false,
    protocol: "anthropic",
  },
];

const PROVIDER = {
  id: "pr1",
  name: "my-vllm",
  description: null,
  base_url: "https://llm.example.org/v1",
  api_key_hint: "sk-m…9876",
  has_api_key: true,
  extra_headers: {},
  is_active: true,
  prefix: "",
  plugin: null,
  kind: "provider",
  plugin_kind: "provider",
  billing_mode: "own_prices",
  catalogue_id: null,
  catalogue: null,
  catalogue_missing: false,
  unpriced_model_count: 0,
  model_count: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};

const MODEL = {
  id: "m1",
  name: "llama",
  upstream_model: "meta/llama-3",
  provider_id: "pr1",
  provider_name: "my-vllm",
  provider_is_active: true,
  provider_kind: "provider",
  kind: "chat",
  display_name: null,
  description: null,
  is_active: true,
  is_public: false,
  context_window: null,
  max_output_tokens: null,
  max_input_tokens: null,
  created_at: "2026-08-01T10:00:00Z",
  current_price: null,
  input_modalities: [],
  output_modalities: [],
  supported_features: [],
  granted_to: [],
  granted_to_users: [],
  aliases_pointing_here: [],
};

const DISCOVERY = {
  provider_url: "https://llm.example.org/v1/models",
  provider_model_count: 1,
  available: [
    {
      upstream_model: "meta/new-1",
      suggested_name: "new-1",
      input_per_mtok: "0.500000000000",
      output_per_mtok: "1.500000000000",
      per_page: null,
      per_minute: null,
      per_image: null,
      per_extra_megapixel: null,
      currency: "EUR",
      context_window: null,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: null,
      price_source: "provider",
      max_input_tokens: null,
      max_output_tokens: null,
    },
  ],
  catalogued: [],
  missing_upstream: [],
  unparsable: [],
};

interface Seen {
  calls: { url: string; method: string; body: unknown }[];
}

/**
 * A gateway with one user's private rows. `createFails` makes the provider
 * create answer the way the network guard does.
 */
function gateway(seen: Seen, createFails = false) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });

    // Plain arrays, not listings: these two answer without a pagination envelope.
    const plain = (payload: unknown) =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    if (url.includes("/provider-plugins")) return plain(PLUGINS);
    if (url.includes("/provider-catalogue")) return plain([]);
    if (url.includes("/models/discover")) return jsonResponse(DISCOVERY);
    if (url.includes("/models/tags"))
      return jsonResponse({ provider_url: "", supported: false, tags: [] });
    if (url.includes("/models/import") && method === "POST")
      return jsonResponse({
        results: [
          {
            upstream_model: "meta/new-1",
            name: "new-1",
            imported: true,
            priced: true,
            reason: null,
            price_source: "provider",
          },
        ],
      });
    if (url.includes("/api/me/providers") && method === "POST") {
      if (createFails)
        return jsonResponse(
          {
            error: {
              message: "the host resolves to a private address (10.0.0.5): rule private",
              type: "invalid_request_error",
              code: "provider_url_blocked",
            },
          },
          400,
        );
      return jsonResponse(PROVIDER, 201);
    }
    if (url.includes("/api/me/providers")) return jsonResponse([PROVIDER]);
    if (url.includes("/api/me/models")) return jsonResponse([MODEL]);
    return jsonResponse([]);
  });
}

function renderPage(user: Me, part: "providers" | "models" | "model" = "providers") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MyProviders me={user} part={part} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("MyProviders", () => {
  it("is shown only with the capability, and calls nothing without it", async () => {
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(false));

    expect(await screen.findByText("Not enabled for your account")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add provider" })).not.toBeInTheDocument();
    expect(seen.calls).toEqual([]);
  });

  it("says who can see the models, and lists only the user's own providers", async () => {
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true));

    expect(
      await screen.findByText(
        "Bring your own key: models you add here run on your own API key. Only you can see or use them. Their usage is shown to you only and doesn't count against your group's limits.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("my-vllm")).toBeInTheDocument();
    // Never the administrators' endpoints: the same screen, another audience.
    expect(seen.calls.every((call) => !call.url.includes("/api/admin/"))).toBe(true);
  });

  it("posts a new provider to /api/me/providers and never renders the key", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true));

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    // The one "Provider" selector, generic OpenAI-compatible and Anthropic last.
    const options = dialog.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["OpenAI-compatible (generic)", "Anthropic (generic)"]);

    await user.type(dialog.getByLabelText("Name"), "my-vllm");
    await user.type(dialog.getByLabelText("Base URL"), "https://llm.example.org/v1");
    const key = dialog.getByLabelText("API key");
    await user.type(key, "sk-very-secret-key");
    // A password field: the key is not shown as it is typed.
    expect(key).toHaveAttribute("type", "password");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(
        seen.calls.some((call) => call.method === "POST" && call.url === "/api/me/providers"),
      ).toBe(true),
    );
    const post = seen.calls.find((call) => call.method === "POST")!;
    expect(post.body).toMatchObject({ name: "my-vllm", api_key: "sk-very-secret-key" });
    // And nowhere on the page, before or after, is the key readable text.
    expect(document.body.textContent).not.toContain("sk-very-secret-key");
  });

  it("explains a refused address instead of showing a bare status", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen, true));
    renderPage(me(true));

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.type(dialog.getByLabelText("Name"), "internal");
    await user.type(dialog.getByLabelText("Base URL"), "http://10.0.0.5/v1");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    // What it means and what to do, then the gateway's own detail.
    expect(
      await dialog.findByText(/private and internal addresses are blocked/i),
    ).toBeInTheDocument();
    expect(dialog.getByText(/ask an administrator to allow private networks/i)).toBeInTheDocument();
    expect(dialog.getByText(/rule private/)).toBeInTheDocument();
  });

  it("tests a connection through /api/me/providers/{id}/test", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true));

    await screen.findByText("my-vllm");
    await user.click(await screen.findByRole("button", { name: "Actions for my-vllm" }));
    await user.click(await screen.findByRole("menuitem", { name: "Test" }));

    await waitFor(() =>
      expect(
        seen.calls.some((call) => call.method === "POST" && call.url === "/api/me/providers/pr1/test"),
      ).toBe(true),
    );
  });
});

describe("MyProviders: models", () => {
  it("shows each model's /v1 name, my/<name>, and offers no grants or public flag", async () => {
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true), "models");

    expect(await screen.findByText("my/llama")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy my/llama" })).toBeInTheDocument();
    // Columns and badges that only a deployment model has.
    expect(screen.queryByText("Access")).not.toBeInTheDocument();
    expect(screen.queryByText("Public")).not.toBeInTheDocument();
  });

  it("imports through /api/me/models/import", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true), "models");

    await user.click(await screen.findByRole("button", { name: "Discover" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Provider"), "pr1");
    await user.click(await screen.findByLabelText("Import new-1"));
    await user.click(dialog.getByRole("button", { name: /^Import/ }));

    await waitFor(() =>
      expect(
        seen.calls.some(
          (call) =>
            call.method === "POST" && call.url.startsWith("/api/me/models/import?provider_id=pr1"),
        ),
      ).toBe(true),
    );
    expect(seen.calls.every((call) => !call.url.includes("/api/admin/"))).toBe(true);
  });

  it("refuses a name starting with my/ before it is sent", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: Seen = { calls: [] };
    vi.stubGlobal("fetch", gateway(seen));
    renderPage(me(true), "models");

    await user.click(await screen.findByRole("button", { name: "Add model" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Provider"), "pr1");
    await user.type(dialog.getByLabelText("Name"), "my/llama");
    await user.type(dialog.getByLabelText("Upstream model"), "meta/llama-3");

    expect(dialog.getByText(/cannot start with my\//i)).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});
