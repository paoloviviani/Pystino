import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminModel, Price } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminModelDetail } from "./AdminModelDetail";

/**
 * The model page: what a model is, what it costs, and who may spend it.
 *
 * Pricing was a screen of its own until this page absorbed it, so the
 * properties worth pinning are the ones that crossed the seam — a price is
 * appended and never edited, cache rates can be set at all, and an unpriced
 * model says so *here*, where someone is already looking at the model.
 *
 * The capability tests came from AdminModels.test.tsx with the editor itself.
 * They are the same properties, checked in the same order; only the way in
 * changed, from a dialog on the listing to the page.
 */

function price(overrides: Partial<Price> = {}): Price {
  return {
    id: "p1",
    input_per_mtok: "1.000000000000",
    output_per_mtok: "2.000000000000",
    per_page: null,
    per_search: null,
    cache_read_per_mtok: null,
    cache_write_per_mtok: null,
    per_image: null,
    currency: "EUR",
    effective_from: "2026-08-01T10:00:00Z",
    source: "manual",
    ...overrides,
  };
}

function model(overrides: Partial<AdminModel> = {}): AdminModel {
  return {
    id: "m1",
    name: "fast-summariser",
    upstream_model: "provider/fast-1",
    provider_id: "pr1",
    provider_name: "acme",
    provider_is_active: true,
    provider_kind: "provider",
    kind: "chat",
    display_name: null,
    description: null,
    is_active: true,
    is_public: false,
    context_window: 8192,
    max_output_tokens: null,
    max_input_tokens: null,
    created_at: "2026-08-01T10:00:00Z",
    current_price: price(),
    input_modalities: ["text"],
    output_modalities: ["text"],
    supported_features: [],
    granted_to: ["research"],
    granted_to_users: [],
    ...overrides,
  };
}

interface Captured {
  bodies: unknown[];
  prices: unknown[];
}

function routes(
  subject: AdminModel,
  history: Price[] = [price()],
  captured: Captured = { bodies: [], prices: [] },
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (method === "PATCH") {
      captured.bodies.push(JSON.parse(String(init?.body)));
      return jsonResponse(subject);
    }
    // Before the bare model route: the price path contains it.
    if (url.includes("/prices")) {
      if (method === "POST") {
        captured.prices.push(JSON.parse(String(init?.body)));
        return jsonResponse(price(), 201);
      }
      return jsonResponse(history);
    }
    if (url.includes("/api/admin/groups")) {
      return jsonResponse([
        {
          id: "g1",
          name: "research",
          description: null,
          source: "idp",
          is_active: true,
          member_count: 2,
          models: ["fast-summariser"],
        },
        {
          id: "g2",
          name: "teaching",
          description: null,
          source: "idp",
          is_active: true,
          member_count: 5,
          models: [],
        },
      ]);
    }
    if (url.includes("/api/admin/users")) return jsonResponse([]);
    if (url.includes("/api/admin/models/")) return jsonResponse(subject);
    return jsonResponse([]);
  });
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/admin/models/m1"]}>
        <Routes>
          <Route path="/admin/models/:modelId" element={<AdminModelDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

/**
 * One capability set, addressed by its legend.
 *
 * Scoping is not tidiness here: "Accepts" and "Produces" both offer a box
 * called `image`, so an unscoped query is ambiguous — which is precisely the
 * confusion the legend exists to resolve for anyone reading the form aloud.
 */
function group(name: string) {
  return within(screen.getByRole("group", { name }));
}

/** The page has loaded the model when its name is the heading. */
async function ready() {
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "fast-summariser" })).toBeInTheDocument(),
  );
}

async function save(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Save capabilities" }));
}


describe("AdminModelDetail", () => {
  it("toggling visibility sends is_public and flips the badge", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model({ is_public: false }), [price()], captured));
    renderScreen();
    await ready();

    expect(screen.getByText("Private")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Make public" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies[0] as Record<string, unknown>;
    expect(body.is_public).toBe(true);
  });

  it("a public model offers to be restricted again", async () => {
    vi.stubGlobal("fetch", routes(model({ is_public: true })));
    renderScreen();
    await ready();
    expect(screen.getByText("Public")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Restrict to grants" }),
    ).toBeInTheDocument();
  });

  it("sends an edited capability set to the API", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model({ supported_features: ["tools"] }), [price()], captured));
    renderScreen();
    await ready();
    await user.click(group("Features").getByRole("checkbox", { name: /^reasoning/ }));
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies[0] as Record<string, unknown>;
    expect(body.supported_features).toEqual(["tools", "reasoning"]);
  });

  it("shows what the model already claims as ticked", async () => {
    vi.stubGlobal(
      "fetch",
      routes(model({ input_modalities: ["text", "image"], supported_features: ["tools"] })),
    );
    renderScreen();
    await ready();

    expect(group("Accepts").getByRole("checkbox", { name: /^image/ })).toBeChecked();
    expect(group("Accepts").getByRole("checkbox", { name: /^audio/ })).not.toBeChecked();
    expect(group("Features").getByRole("checkbox", { name: /^tools/ })).toBeChecked();
    expect(group("Features").getByRole("checkbox", { name: /^reasoning/ })).not.toBeChecked();
  });

  it("unticking a box takes the capability away", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal(
      "fetch",
      routes(model({ supported_features: ["tools", "reasoning"] }), [price()], captured),
    );
    renderScreen();
    await ready();
    await user.click(group("Features").getByRole("checkbox", { name: /^tools/ }));
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0] as Record<string, unknown>).supported_features).toEqual([
      "reasoning",
    ]);
  });

  it("keeps a capability the provider invented that has no checkbox", async () => {
    // The failure this whole design exists to prevent (ADR 0031). A value with
    // nowhere to render would be dropped by the first save, so an operator
    // would destroy information by opening the page and clicking Save without
    // touching anything.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal(
      "fetch",
      routes(model({ supported_features: ["tools", "web_search"] }), [price()], captured),
    );
    renderScreen();
    await ready();
    // Visible and editable, not merely retained behind the scenes.
    expect(group("Features").getByLabelText("Other features")).toHaveValue("web_search");

    await user.click(group("Features").getByRole("checkbox", { name: /^reasoning/ }));
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0] as Record<string, unknown>).supported_features).toEqual([
      "tools",
      "reasoning",
      "web_search",
    ]);
  });

  it("adds an unrecognised capability typed by hand", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model({ supported_features: ["tools"] }), [price()], captured));
    renderScreen();
    await ready();
    await user.type(group("Features").getByLabelText("Other features"), "web_search, citations");
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0] as Record<string, unknown>).supported_features).toEqual([
      "tools",
      "web_search",
      "citations",
    ]);
  });

  it("does not list a value twice when it is both ticked and typed", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model({ supported_features: ["tools"] }), [price()], captured));
    renderScreen();
    await ready();
    await user.type(group("Features").getByLabelText("Other features"), "tools");
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0] as Record<string, unknown>).supported_features).toEqual(["tools"]);
  });

  it("keeps the accepts and produces sets apart", async () => {
    // Both offer a box called "image", and they mean different things. If the
    // legend did not name the group they would be indistinguishable — to a
    // screen reader as much as to this test.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model({ output_modalities: ["text"] }), [price()], captured));
    renderScreen();
    await ready();
    await user.click(group("Accepts").getByRole("checkbox", { name: /^image/ }));
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies[0] as Record<string, unknown>;
    expect(body.input_modalities).toEqual(["text", "image"]);
    expect(body.output_modalities).toEqual(["text"]);
  });

  it("can correct a mis-inferred kind", async () => {
    // Discovery reads the kind off modality tags. Getting it wrong takes the
    // model off the only route that would serve it.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model(), [price()], captured));
    renderScreen();
    await ready();
    await user.selectOptions(screen.getByLabelText("Kind"), "embedding");
    await save(user);

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0] as Record<string, unknown>).kind).toBe("embedding");
  });

  it("shows the price history beside the model, not on another screen", async () => {
    vi.stubGlobal(
      "fetch",
      routes(model(), [
        price({ id: "p2", input_per_mtok: "3.000000000000", effective_from: "2026-09-01T10:00:00Z" }),
        price(),
      ]),
    );
    renderScreen();
    await ready();

    const history = within(await screen.findByRole("table"));
    expect(history.getByText("€3.00")).toBeInTheDocument();
    expect(history.getByText("€1.00")).toBeInTheDocument();
  });

  it("marks a price that has not taken effect yet", async () => {
    // A scheduled price is not the current one, and a table that showed both
    // identically would have an operator reading next month's rate as today's.
    const future = new Date(Date.now() + 86_400_000).toISOString();
    vi.stubGlobal("fetch", routes(model(), [price({ effective_from: future })]));
    renderScreen();
    await ready();

    expect(await screen.findByText("Scheduled")).toBeInTheDocument();
  });

  it("warns on the model's own page that it has no price", async () => {
    // The dangerous state: catalogued, granted, unpriced — it serves happily
    // and records a cost of zero. It used to be visible only in the listing or
    // on a separate pricing screen nobody had a reason to open.
    vi.stubGlobal("fetch", routes(model({ current_price: null }), []));
    renderScreen();
    await ready();

    expect(await screen.findByText("This model has no price")).toBeInTheDocument();
  });

  it("appends a price, including the cache rates no screen used to offer", async () => {
    // The API has always accepted cache rates. Without them a provider that
    // bills cache reads at a fraction of the input rate is billed here at the
    // full one, so the ledger and the invoice diverge on exactly the requests
    // the cache was meant to make cheaper.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model(), [price()], captured));
    renderScreen();
    await ready();

    await user.type(screen.getByLabelText("Input per Mtok"), "1.5");
    await user.type(screen.getByLabelText("Output per Mtok"), "3");
    await user.type(screen.getByLabelText("Cache read per Mtok"), "0.15");
    await user.click(screen.getByRole("button", { name: "Append price" }));

    await waitFor(() => expect(captured.prices.length).toBe(1));
    const body = captured.prices[0] as Record<string, unknown>;
    expect(body.input_per_mtok).toBe("1.5");
    expect(body.cache_read_per_mtok).toBe("0.15");
    // Omitted rather than zero: "not priced this way" and "free" are different
    // facts, and one of them is a decision somebody made.
    expect(body.cache_write_per_mtok).toBeNull();
    expect(body.per_image).toBeNull();
  });

  it("will not append a price with a rate missing", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { bodies: [], prices: [] };
    vi.stubGlobal("fetch", routes(model(), [price()], captured));
    renderScreen();
    await ready();

    await user.type(screen.getByLabelText("Input per Mtok"), "1.5");
    expect(screen.getByRole("button", { name: "Append price" })).toBeDisabled();
  });

  it("grants and revokes a group from the same page", async () => {
    const user = userEvent.setup({ delay: null });
    const calls: string[] = [];
    const base = routes(model());
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/models/m1") && (init?.method === "PUT" || init?.method === "DELETE")) {
          calls.push(`${init?.method} ${url}`);
          return new Response(null, { status: 204 });
        }
        return base(input, init);
      }),
    );
    renderScreen();
    await ready();

    // `research` already has it, `teaching` does not — so one click is a
    // revocation and the other a grant, and they are different verbs.
    await user.click(await screen.findByRole("checkbox", { name: "teaching" }));
    await waitFor(() => expect(calls).toEqual(["PUT /api/admin/groups/g2/models/m1"]));

    await user.click(screen.getByRole("checkbox", { name: "research" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toBe("DELETE /api/admin/groups/g1/models/m1");
  });

  it("says when the provider behind the model is switched off", async () => {
    // The model's own status says Active and it serves nobody. Without this the
    // page would be confidently wrong about the only thing it is asked.
    vi.stubGlobal("fetch", routes(model({ provider_is_active: false })));
    renderScreen();
    await ready();

    expect(await screen.findByText("acme is deactivated")).toBeInTheDocument();
  });
});
