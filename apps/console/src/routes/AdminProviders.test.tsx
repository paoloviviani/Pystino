import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminProvider } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminProviders } from "./AdminProviders";

/**
 * The provider screen handles credentials, so the properties worth pinning are
 * about what it does *not* do: never show a key, never send one the operator
 * did not type, and never wipe a stored key as a side effect of editing
 * something else.
 */

function provider(overrides: Partial<AdminProvider> = {}): AdminProvider {
  return {
    id: "pr1",
    name: "acme",
    description: "Commercial API",
    base_url: "https://acme.test/v1",
    api_key_hint: "sk-a…3456",
    has_api_key: true,
    extra_headers: {},
    is_active: true,
    prefix: "",
    plugin: null,
    kind: "provider",
    plugin_kind: "provider",
    billing_mode: "own_prices",
    unpriced_model_count: 0,
    model_count: 3,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

interface Captured {
  bodies: { url: string; body: unknown }[];
}

/**
 * The installed provider types, as the registry would report them.
 *
 * Returned raw rather than through `jsonResponse`, which wraps an array in the
 * pagination envelope every *listing* uses — this endpoint is a handful of
 * installed packages, not a listing, so it answers with a plain array.
 */
const PLUGINS = [
  {
    name: "generic",
    label: "OpenAI-compatible",
    description: "Forwards requests unchanged. Tokens are counted here.",
    kind: "provider",
    billing_modes: ["own_prices"],
    default_base_url: null,
    base_url_options: [],
    is_default: true,
  },
  {
    name: "openai",
    label: "OpenAI",
    description: "OpenAI's own API.",
    kind: "provider",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.openai.com/v1",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "cortecs",
    label: "Cortecs (router)",
    description: "Chooses a sub-provider per request and reports its own cost.",
    kind: "router",
    billing_modes: ["own_prices", "provider_reported"],
    default_base_url: "https://api.cortecs.ai/v1",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "exa",
    label: "Exa (web search)",
    description: "Exa web search.",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.exa.ai",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "jina",
    label: "Jina (web search)",
    description: "Jina web search.",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://s.jina.ai",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "linkup",
    label: "Linkup (web search)",
    description: "Linkup web search.",
    kind: "search",
    billing_modes: ["own_prices"],
    default_base_url: "https://api.linkup.so/v1",
    base_url_options: [],
    is_default: false,
  },
  {
    name: "extractor",
    label: "Extractor",
    description: "The deployment's own document extractor.",
    kind: "internal",
    billing_modes: ["own_prices"],
    default_base_url: null,
    base_url_options: [],
    is_default: false,
  },
];

function routes(providers: AdminProvider[], captured: Captured = { bodies: [] }, test?: unknown) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (init?.body) captured.bodies.push({ url, body: JSON.parse(String(init.body)) });

    if (url.includes("/provider-plugins")) {
      return new Response(JSON.stringify(PLUGINS), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }

    let payload: unknown = [];
    if (url.endsWith("/test") && method === "POST") {
      payload = test ?? { ok: true, status_code: 200, detail: "reachable", model_count: 4, sample: [], latency_ms: 42 };
    } else if (url.includes("/api/admin/providers")) {
      payload = method === "GET" ? providers : providers[0];
    }
    return jsonResponse(payload);
  });
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminProviders", () => {
  it("lists a provider with its endpoint", async () => {
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    expect(screen.getByText("https://acme.test/v1")).toBeInTheDocument();
  });

  it("shows only a hint of the key, never the key", async () => {
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("sk-a…3456")).toBeInTheDocument());
  });

  it("says when a provider has no credential", async () => {
    // A local vLLM or Ollama usually needs none, and "none" is different from
    // "we lost it".
    vi.stubGlobal("fetch", routes([provider({ has_api_key: false, api_key_hint: "" })]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("none")).toBeInTheDocument());
  });

  it("shows how many models a provider serves", async () => {
    // The blast radius of deactivating or deleting it.
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
  });

  it("reports a successful test with what the provider offers", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(screen.getByText(/4 models/)).toBeInTheDocument());
  });

  it("shows why a test failed rather than a generic error", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routes([provider()], { bodies: [] }, {
        ok: false,
        status_code: 401,
        detail: "the provider answered 401 — check the API key",
        model_count: null,
        sample: [],
        latency_ms: 88,
      }),
    );
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(
        screen.getByText("the provider answered 401 — check the API key"),
      ).toBeInTheDocument(),
    );
  });

  it("does not send an api_key when the field was left alone", async () => {
    // The three-way convention. Editing a base URL must not wipe the stored
    // credential, and sending "" would do exactly that.
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    const url = within(dialog).getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "https://acme.test/v2");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies[0]?.body as Record<string, unknown>;
    expect(body.base_url).toBe("https://acme.test/v2");
    expect("api_key" in body).toBe(false);
  });

  it("sends an empty api_key only when removal is chosen explicitly", async () => {
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Remove the stored key"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0]?.body as Record<string, unknown>).api_key).toBe("");
  });

  it("sends a typed key as the replacement", async () => {
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("API key"), "sk-new-999999");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0]?.body as Record<string, unknown>).api_key).toBe("sk-new-999999");
  });

  it("says plainly when nothing is configured", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminProviders />);

    await waitFor(() =>
      expect(
        screen.getByText("No providers configured. Nothing can be served until one exists."),
      ).toBeInTheDocument(),
    );
  });

  it("will not create a provider without a name and endpoint", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminProviders />);

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();

    await user.type(within(dialog).getByLabelText("Name"), "ollama");
    await user.type(within(dialog).getByLabelText("Base URL"), "http://ollama:11434/v1");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeEnabled();
  });
});

describe("AdminProviders: choosing a type", () => {
  it("offers the installed types rather than a hardcoded list", async () => {
    // Read from the API so installing a plugin makes it selectable without a
    // console release, which is the point of the entry point existing.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    const type = dialog.getByLabelText("Type");
    expect(within(type).getByRole("option", { name: /OpenAI-compatible/ })).toBeInTheDocument();
    expect(within(type).getByRole("option", { name: /Cortecs/ })).toBeInTheDocument();
  });

  it("offers only LLM-provider kinds when creating — no search, no plumbing", async () => {
    // Search backends are created on the Search screen (ADR 0071) and the
    // extractor is the deployment's own plumbing: neither belongs in an
    // inference-provider picker.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    const type = dialog.getByLabelText("Type");
    const options = within(type).getAllByRole("option");
    const names = options.map((option) => option.textContent ?? "");
    expect(names.some((text) => /OpenAI-compatible/.test(text))).toBe(true);
    expect(names.some((text) => /OpenAI/.test(text))).toBe(true);
    expect(names.some((text) => /Cortecs/.test(text))).toBe(true);
    expect(names.some((text) => /Exa/.test(text))).toBe(false);
    expect(names.some((text) => /Jina/.test(text))).toBe(false);
    expect(names.some((text) => /Linkup/.test(text))).toBe(false);
    expect(names.some((text) => /Extractor/.test(text))).toBe(false);
  });

  it("keeps the row's own type selectable when editing past the filter", async () => {
    // The extractor row is managed here but its kind is not offered on
    // create: opening it must not blank its Type.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      routes([provider({ id: "px", name: "extract", plugin: "extractor", kind: "internal", plugin_kind: "internal" })]),
    );
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("extract")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    const type = dialog.getByLabelText("Type") as HTMLSelectElement;
    const names = [...type.options].map((option) => option.text);
    expect(names.some((text) => /Extractor/.test(text))).toBe(true);
    expect(type.value).toBe("extractor");
  });

  it("explains what the chosen type means for billing", async () => {
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/Tokens are counted here/)).toBeInTheDocument();
  });

  it("only offers pass-through billing for a type that can support it", async () => {
    // The generic type reads no authoritative figure, so offering the choice
    // would be offering a configuration the API refuses.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.queryByLabelText("Billing")).not.toBeInTheDocument();

    await user.selectOptions(dialog.getByLabelText("Type"), "cortecs");
    expect(dialog.getByLabelText("Billing")).toBeInTheDocument();
  });

  it("warns that pass-through still needs prices", async () => {
    // Admission happens before the request; the provider's figure arrives
    // after. An unpriced model reserves nothing and no ceiling ever trips.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.selectOptions(dialog.getByLabelText("Type"), "cortecs");
    await user.selectOptions(dialog.getByLabelText("Billing"), "provider_reported");

    expect(dialog.getByText(/Prices are still needed/)).toBeInTheDocument();
  });

  it("sends the kind implied by the type, not one the operator typed", async () => {
    // Whether the serving endpoint is chosen per request is a property of the
    // counterparty, not an opinion an operator should have to hold.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.type(dialog.getByLabelText("Name"), "cx");
    await user.type(dialog.getByLabelText("Base URL"), "https://api.cortecs.ai/v1");
    await user.selectOptions(dialog.getByLabelText("Type"), "cortecs");
    await user.click(dialog.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies.at(-1)!.body as Record<string, unknown>;
    expect(body.plugin).toBe("cortecs");
    expect(body.kind).toBe("router");
  });

  it("fills the endpoint in from the type, so Cortecs is a name and a key", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.type(dialog.getByLabelText("Name"), "cortecs");
    await user.selectOptions(dialog.getByLabelText("Type"), "cortecs");

    const url = dialog.getByLabelText("Base URL") as HTMLInputElement;
    expect(url.value).toBe("https://api.cortecs.ai/v1");

    // The dialog can be submitted without typing the endpoint at all: the
    // field was filled by the type, and the API would apply the same default
    // if it were cleared.
    await user.click(dialog.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies.at(-1)!.body as Record<string, unknown>;
    expect(body.base_url).toBe("https://api.cortecs.ai/v1");
  });

  it("does not clobber an endpoint the operator typed themselves", async () => {
    // A URL that is not some plugin's default is the operator's own — a
    // private gateway, a proxy — and choosing a type afterwards must leave it.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.type(dialog.getByLabelText("Base URL"), "https://cortecs.internal.test/v1");
    await user.selectOptions(dialog.getByLabelText("Type"), "cortecs");

    expect((dialog.getByLabelText("Base URL") as HTMLInputElement).value).toBe(
      "https://cortecs.internal.test/v1",
    );
  });

  it("shows what each provider is, and flags what needs attention", async () => {
    vi.stubGlobal(
      "fetch",
      routes([
        provider({
          plugin: "cortecs",
          kind: "router",
          plugin_kind: "router",
          billing_mode: "provider_reported",
          unpriced_model_count: 2,
        }),
      ]),
    );
    renderScreen(<AdminProviders />);

    const table = within(await screen.findByRole("table"));
    // The *label*, not the plugin's internal name. Waiting on the label is also
    // what makes this deterministic: until the type list arrives the cell falls
    // back to the raw name, so asserting "cortecs" was asserting a state that
    // exists only before the query settles.
    await waitFor(() => expect(table.getByText("Cortecs (router)")).toBeInTheDocument());
    expect(table.getByText("router")).toBeInTheDocument();
    expect(table.getByText("bills from provider")).toBeInTheDocument();
    expect(table.getByText("2 unpriced")).toBeInTheDocument();
  });

  it("flags a plugin whose kind disagrees with the stored one", async () => {
    // Naming the router plugin while the row still says `provider` is a
    // configuration to point at rather than a silent inconsistency.
    vi.stubGlobal(
      "fetch",
      routes([provider({ plugin: "cortecs", kind: "provider", plugin_kind: "router" })]),
    );
    renderScreen(<AdminProviders />);

    const table = within(await screen.findByRole("table"));
    await waitFor(() => expect(table.getByText("kind mismatch")).toBeInTheDocument());
  });
});

describe("AdminProviders: the local extractor cannot be deleted", () => {
  it("disables Delete for an internal-kind provider", async () => {
    // The gateway answers 409 either way (`routers/admin.py`); disabling the
    // button here means an administrator sees that up front rather than
    // after a click and an error toast.
    vi.stubGlobal(
      "fetch",
      routes([
        provider({ name: "extractor", plugin: "extractor", kind: "internal", plugin_kind: "internal" }),
      ]),
    );
    renderScreen(<AdminProviders />);

    const table = within(await screen.findByRole("table"));
    await waitFor(() => expect(table.getByText("extractor")).toBeInTheDocument());
    expect(table.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("leaves Delete enabled for an ordinary provider", async () => {
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    const table = within(await screen.findByRole("table"));
    await waitFor(() => expect(table.getByText("acme")).toBeInTheDocument());
    expect(table.getByRole("button", { name: "Delete" })).toBeEnabled();
  });

  it("still offers Deactivate for the internal provider", async () => {
    vi.stubGlobal(
      "fetch",
      routes([
        provider({ name: "extractor", plugin: "extractor", kind: "internal", plugin_kind: "internal" }),
      ]),
    );
    renderScreen(<AdminProviders />);

    const table = within(await screen.findByRole("table"));
    await waitFor(() => expect(table.getByText("extractor")).toBeInTheDocument());
    expect(table.getByRole("button", { name: "Deactivate" })).toBeEnabled();
  });
});
