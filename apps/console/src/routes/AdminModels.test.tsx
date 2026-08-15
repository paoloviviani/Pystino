import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminModel, CatalogueDiscovery } from "../lib/types";
import { AdminModels } from "./AdminModels";

/**
 * The catalogue screen decides what users can spend money on, so the properties
 * worth pinning are the safety ones: an unpriced model is visibly flagged, a
 * model nobody can reach says so, drift in the dangerous direction is surfaced,
 * and nothing is imported without an explicit choice.
 */

function model(overrides: Partial<AdminModel> = {}): AdminModel {
  return {
    id: "m1",
    name: "fast-summariser",
    upstream_model: "provider/fast-1",
    provider_id: "pr1",
    provider_name: "acme",
    provider_is_active: true,
    kind: "chat",
    display_name: null,
    description: null,
    is_active: true,
    context_window: 8192,
    max_output_tokens: null,
    created_at: "2026-08-01T10:00:00Z",
    current_price: {
      id: "p1",
      input_per_mtok: "1.000000000000",
      output_per_mtok: "2.000000000000",
      cache_read_per_mtok: null,
      cache_write_per_mtok: null,
      currency: "EUR",
      effective_from: "2026-08-01T10:00:00Z",
      source: "manual",
    },
    granted_to: ["research"],
    granted_to_users: [],
    ...overrides,
  };
}

const DISCOVERY: CatalogueDiscovery = {
  provider_url: "http://provider/v1/models",
  provider_model_count: 3,
  available: [
    {
      upstream_model: "provider/new-1",
      suggested_name: "new-1",
      input_per_mtok: "0.500000000000",
      output_per_mtok: "1.500000000000",
      currency: "EUR",
      context_window: 32000,
      blocked_reason: null,
    },
    {
      upstream_model: "provider/dollar-1",
      suggested_name: "dollar-1",
      input_per_mtok: "0.900000000000",
      output_per_mtok: "1.900000000000",
      currency: "USD",
      context_window: null,
      blocked_reason: "priced in USD; this gateway bills in EUR",
    },
  ],
  catalogued: [],
  missing_upstream: [
    { name: "retired-model", upstream_model: "provider/retired", is_active: true },
  ],
  unparsable: [],
};

function routes(models: AdminModel[], calls: { imported?: string[] } = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    let payload: unknown = [];

    if (url.includes("/api/admin/providers")) {
      payload = [
        {
          id: "pr1",
          name: "acme",
          description: null,
          base_url: "https://acme.test/v1",
          api_key_hint: "sk-a…3456",
          has_api_key: true,
          extra_headers: {},
          is_active: true,
          forward_stream_options: true,
          model_count: 1,
          created_at: "2026-08-01T10:00:00Z",
          updated_at: "2026-08-01T10:00:00Z",
        },
      ];
    } else if (url.includes("/models/discover")) payload = DISCOVERY;
    else if (url.includes("/models/import") && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { models: { upstream_model: string }[] };
      calls.imported = body.models.map((entry) => entry.upstream_model);
      payload = {
        results: body.models.map((entry) => ({
          upstream_model: entry.upstream_model,
          name: "new-1",
          imported: true,
          priced: true,
          reason: null,
        })),
      };
    } else if (url.includes("/api/admin/models")) payload = models;
    else if (url.includes("/api/admin/users")) payload = [];
    else if (url.includes("/api/admin/groups")) {
      payload = [
        { id: "g1", name: "research", description: null, source: "idp", is_active: true, member_count: 2, models: ["fast-summariser"] },
        { id: "g2", name: "teaching", description: null, source: "idp", is_active: true, member_count: 5, models: [] },
      ];
    }

    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
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

/** Pick the provider whose catalogue to inspect; nothing loads before that. */
async function openCatalogue(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog");
  await user.selectOptions(within(dialog).getByLabelText("Provider"), "pr1");
}

describe("AdminModels", () => {
  it("lists a model with its current price", async () => {
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    expect(screen.getByText("€1.00 in")).toBeInTheDocument();
    expect(screen.getByText("€2.00 out")).toBeInTheDocument();
  });

  it("flags an unpriced model", async () => {
    // An unpriced model serves happily and records a cost of zero, which is a
    // quiet way to give money away. It must be visible in the list.
    vi.stubGlobal("fetch", routes([model({ current_price: null })]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("No price")).toBeInTheDocument());
  });

  it("says when a model is granted to nobody", async () => {
    vi.stubGlobal("fetch", routes([model({ granted_to: [] })]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("nobody")).toBeInTheDocument());
  });

  it("does not ask the provider until asked", async () => {
    // Discovery calls out over the network. A page that hangs on load because a
    // third party is slow is worse than one with a button on it.
    const fetchMock = routes([model()]);
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    expect(fetchMock.mock.calls.map(String).some((url) => url.includes("discover"))).toBe(false);
  });

  it("does not fetch a catalogue until a provider is chosen", async () => {
    // "What is on offer" is only meaningful about one endpoint (ADR 0027).
    const user = userEvent.setup();
    const fetchMock = routes([model()]);
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await screen.findByRole("dialog");
    expect(fetchMock.mock.calls.map(String).some((url) => url.includes("discover"))).toBe(false);
  });

  it("surfaces models we still serve that the provider has dropped", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);

    await waitFor(() =>
      expect(screen.getByText(/no longer offered upstream/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/retired-model/)).toBeInTheDocument();
  });

  it("refuses to import a model priced in another currency", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    const checkbox = await screen.findByLabelText("Import dollar-1");
    expect(checkbox).toBeDisabled();
    expect(screen.getByText(/priced in USD/)).toBeInTheDocument();
  });

  it("imports only what was explicitly selected", async () => {
    // Auto-adopting whatever a provider publishes would let their release notes
    // change what users can spend money on.
    const user = userEvent.setup();
    const calls: { imported?: string[] } = {};
    vi.stubGlobal("fetch", routes([model()], calls));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    const dialog = await screen.findByRole("dialog");

    const importButton = within(dialog).getByRole("button", { name: /^Import/ });
    expect(importButton).toBeDisabled();

    await user.click(await screen.findByLabelText("Import new-1"));
    await user.click(within(dialog).getByRole("button", { name: /^Import/ }));

    await waitFor(() => expect(calls.imported).toEqual(["provider/new-1"]));
  });

  it("sends the chosen provider with the import", async () => {
    const user = userEvent.setup();
    const seen: string[] = [];
    const base = routes([model()]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        seen.push(String(input));
        return base(input, init);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(await screen.findByLabelText("Import new-1"));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: /^Import/ }),
    );

    await waitFor(() =>
      expect(seen.some((url) => url.includes("/models/import?provider_id=pr1"))).toBe(true),
    );
  });
});
