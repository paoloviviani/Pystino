import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminModel, CatalogueDiscovery, CatalogueTags } from "../lib/types";
import { jsonResponse } from "../test-helpers";
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
    current_price: {
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
    },
    input_modalities: ["text"],
    output_modalities: ["text"],
    supported_features: [],
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
      per_page: null,
      currency: "EUR",
      context_window: 32000,
      kind: "chat",
      input_modalities: ["text", "image"],
      output_modalities: ["text"],
      supported_features: ["tools"],
      blocked_reason: null,
      price_source: "provider",
    },
    {
      upstream_model: "provider/dollar-1",
      suggested_name: "dollar-1",
      input_per_mtok: "0.900000000000",
      output_per_mtok: "1.900000000000",
      per_page: null,
      currency: "USD",
      context_window: null,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      // ADR 0054: no longer blocked — the USD prices come along and the
      // conversion happens at admission.
      blocked_reason: null,
      price_source: "provider",
    },
  ],
  catalogued: [],
  missing_upstream: [
    { id: "m-retired", name: "retired-model", upstream_model: "provider/retired", is_active: true },
  ],
  unparsable: [],
};

/** The same provider, asked with the community fill on.
 *
 * `provider/new-1` keeps the price the provider published — the fill must never
 * replace one — `provider/unpriced-1` gains one from the community file, and
 * `provider/nowhere-1` is listed by the provider and priced by nobody.
 */
const DISCOVERY_FILLED: CatalogueDiscovery = {
  ...DISCOVERY,
  available: [
    ...DISCOVERY.available,
    {
      upstream_model: "provider/unpriced-1",
      suggested_name: "unpriced-1",
      input_per_mtok: "0.250000000000",
      output_per_mtok: "0.750000000000",
      per_page: null,
      currency: "USD",
      context_window: 8000,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: null,
      price_source: "community",
    },
    {
      upstream_model: "provider/nowhere-1",
      suggested_name: "nowhere-1",
      input_per_mtok: null,
      output_per_mtok: null,
      per_page: null,
      currency: null,
      context_window: null,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: "the provider publishes no price for this model",
      price_source: null,
    },
  ],
};

/** What `/models/tags` answers for a provider whose plugin knows the fetch.
 *
 * The counterparty's spelling is kept — the values go back as `tag=…`.
 */
const TAGS: CatalogueTags = {
  provider_url: "https://cortecs.test/v1/models?tag=all",
  supported: true,
  tags: ["Embedding", "Instruct", "OCR"],
};

function routes(
  models: AdminModel[],
  calls: {
    imported?: string[];
    discoverUrls?: string[];
    importUrls?: string[];
    tagUrls?: string[];
  } = {},
  captured: { bodies: unknown[] } = { bodies: [] },
  /** What `/models/tags` answers. Null means the provider reports no known
   * vocabulary, which is every provider in the older tests. */
  tags: CatalogueTags | null = null,
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    let payload: unknown = [];

    if (init?.body && method === "PATCH") {
      captured.bodies.push(JSON.parse(String(init.body)));
      return jsonResponse(models[0]);
    }

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
          model_count: 1,
          created_at: "2026-08-01T10:00:00Z",
          updated_at: "2026-08-01T10:00:00Z",
        },
      ];
    } else if (url.includes("/models/discover")) {
      (calls.discoverUrls ??= []).push(url);
      payload = url.includes("fill_missing_prices=true") ? DISCOVERY_FILLED : DISCOVERY;
    } else if (url.includes("/models/import") && method === "POST") {
      (calls.importUrls ??= []).push(url);
      const body = JSON.parse(String(init?.body)) as { models: { upstream_model: string }[] };
      calls.imported = body.models.map((entry) => entry.upstream_model);
      payload = {
        results: body.models.map((entry) => ({
          upstream_model: entry.upstream_model,
          name: "new-1",
          imported: true,
          priced: true,
          reason: null,
          price_source: url.includes("fill_missing_prices=true") ? "community" : "provider",
        })),
      };
    } else if (url.includes("/models/tags")) {
      (calls.tagUrls ??= []).push(url);
      // Checked before the bare `/api/admin/models` branch: the tags URL
      // contains that prefix too, and an array is not an answer a
      // `CatalogueTags` reader can look at.
      payload = tags ?? { provider_url: "", supported: false, tags: [] };
    } else if (url.includes("/api/admin/models")) payload = models;
    else if (url.includes("/api/admin/users")) payload = [];
    else if (url.includes("/api/admin/groups")) {
      payload = [
        { id: "g1", name: "research", description: null, source: "idp", is_active: true, member_count: 2, models: ["fast-summariser"] },
        { id: "g2", name: "teaching", description: null, source: "idp", is_active: true, member_count: 5, models: [] },
      ];
    }

    return jsonResponse(payload);
  });
}

/**
 * Where the router went, for the tests that assert on navigation.
 *
 * MemoryRouter keeps its location to itself and nothing on screen spells the
 * path out, so a component that reports it is the only way to tell "Edit opened
 * the model" from "Edit did nothing at all".
 */
const location = { pathname: "/" };

function Probe() {
  location.pathname = useLocation().pathname;
  return null;
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  location.pathname = "/";
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        {element}
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

/** Pick the provider whose catalogue to inspect; nothing loads before that.
 *
 * Choosing the provider is the only step needed to see a catalogue. Whether to
 * fill missing prices from the community file is a checkbox on this same
 * screen, so there is no chooser to get past first (ADR 0053).
 */
async function openCatalogue(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog");
  await user.selectOptions(within(dialog).getByLabelText("Provider"), "pr1");
}

describe("AdminModels", () => {
  it("groups the catalogue by what each model is for", async () => {
    // A chat model, an embedding model and an OCR model are not alternatives
    // an operator picks between — they are different things in one table, and
    // the columns that matter differ between them.
    vi.stubGlobal(
      "fetch",
      routes([
        model(),
        model({ id: "m2", name: "vectoriser", kind: "embedding" }),
        model({ id: "m3", name: "reader", kind: "ocr" }),
      ]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /^Chat/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^Embedding/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^Document extraction/ })).toBeInTheDocument();
    // Nothing of that kind, so no empty section: "no image models" is not
    // information anybody needs on this screen.
    expect(screen.queryByRole("heading", { name: /^Image/ })).not.toBeInTheDocument();
  });

  it("counts each section, so a long catalogue is legible at a glance", async () => {
    vi.stubGlobal(
      "fetch",
      routes([model(), model({ id: "m2", name: "second-chat" })]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("second-chat")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /^Chat/ })).toHaveTextContent("(2)");
  });

  it("shows a kind the console has never heard of rather than hiding it", async () => {
    // The gateway's enum can gain a value before this file does, and a model
    // that exists but renders nowhere is worse than one under a raw heading.
    vi.stubGlobal(
      "fetch",
      routes([model({ id: "m9", name: "future-thing", kind: "rerank" as never })]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("future-thing")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /^Other/ })).toBeInTheDocument();
  });

  it("does not render the deployment's own infrastructure as a catalogue row", async () => {
    // The extractor's `markitdown` row is seeded by default (migration 0041)
    // and exists so grants, prices and the ledger have something to hang on;
    // rendering it beside the catalogue presents plumbing as a choice, with a
    // "no price" warning that is the design and not an oversight. The admin
    // API still returns the row, and marks it `provider_kind: "internal"` —
    // hidden here is presentation, exactly as the search tiers above.
    vi.stubGlobal(
      "fetch",
      routes([
        model(),
        model({
          id: "m7",
          name: "markitdown",
          upstream_model: "markitdown",
          provider_name: "extractor",
          provider_kind: "internal",
          kind: "ocr",
          current_price: null,
          granted_to: [],
        }),
      ]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    expect(screen.queryByText("markitdown")).not.toBeInTheDocument();
  });

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

  it("shows what a model can do", async () => {
    vi.stubGlobal(
      "fetch",
      routes([
        model({
          input_modalities: ["image", "text"],
          supported_features: ["json_mode", "tools"],
        }),
      ]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    // Scoped to the listing: the Add dialog is mounted from the start, closed,
    // and its capability boxes are called "image" and "text" too.
    const listing = within(screen.getByRole("table"));
    // "text" is not shown: every model takes text, so a badge for it is noise
    // that crowds out the ones that distinguish models from each other.
    expect(listing.getByText("image")).toBeInTheDocument();
    expect(listing.getByText("tools")).toBeInTheDocument();
    expect(listing.queryByText("text")).not.toBeInTheDocument();
  });

  it("says nothing was stated rather than implying a model cannot", async () => {
    // Most catalogued models predate capabilities. Rendering that as crosses
    // would turn an absence of information into a claim.
    vi.stubGlobal("fetch", routes([model({ input_modalities: [], supported_features: [] })]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("not stated")).toBeInTheDocument());
  });

  it("links each model to its own page, by name and by button", async () => {
    // Both, deliberately. The name is what someone points at and is a real
    // anchor, so "open in a new tab" works; the button is what the eye finds in
    // a row of actions. Everything that configures a model — capabilities,
    // price, access — is behind it.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    const link = await screen.findByRole("link", { name: "fast-summariser" });
    expect(link).toHaveAttribute("href", "/admin/models/m1");

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(location.pathname).toBe("/admin/models/m1");
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

  it("asks the provider alone until the community fill is ticked", async () => {
    // The flow's shape, asserted: choosing a provider is enough to see a
    // catalogue, and the fill is a refinement of that answer rather than a
    // question standing in front of it. It was a modal asked before the
    // provider was known, where picking wrong returned an empty screen.
    const user = userEvent.setup();
    const calls: { discoverUrls?: string[] } = {};
    vi.stubGlobal("fetch", routes([model()], calls));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await waitFor(() => expect(screen.getByText("new-1")).toBeInTheDocument());

    expect(calls.discoverUrls?.[0]).toContain("fill_missing_prices=false");
    expect(screen.queryByText("community")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));
    await waitFor(() => expect(screen.getByText("unpriced-1")).toBeInTheDocument());
    expect(calls.discoverUrls?.some((url) => url.includes("fill_missing_prices=true"))).toBe(true);
  });

  it("says which prices a third party supplied, and which the provider did", async () => {
    // The point of the tick is that some of these figures come from a community
    // file. A screen that mixes them without saying so invites an operator to
    // read all of them as the counterparty's own.
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));

    await waitFor(() => expect(screen.getByText("unpriced-1")).toBeInTheDocument());
    // Exactly one row is marked: the filled one. The provider's own prices are
    // unmarked, which is what makes the mark mean something.
    expect(screen.getAllByText("community")).toHaveLength(1);
    // And a model nobody prices cannot be selected for import.
    expect(screen.getByRole("checkbox", { name: /Import nowhere-1/i })).toBeDisabled();
  });

  it("imports with the same fill the prices were reviewed under", async () => {
    // Otherwise the figures written to the append-only history are not the
    // figures anyone approved.
    const user = userEvent.setup();
    const calls: { imported?: string[]; importUrls?: string[] } = {};
    vi.stubGlobal("fetch", routes([model()], calls));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));
    await waitFor(() => expect(screen.getByText("unpriced-1")).toBeInTheDocument());

    await user.click(screen.getByRole("checkbox", { name: "Import unpriced-1" }));
    await user.click(screen.getByRole("button", { name: /^Import/ }));

    await waitFor(() => expect(calls.imported).toEqual(["provider/unpriced-1"]));
    expect(calls.importUrls?.[0]).toContain("fill_missing_prices=true");
    expect(await screen.findByText(/with a community price/i)).toBeInTheDocument();
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
    // Both halves, in the direction that says which is which. "retired-model
    // (provider/retired)" reads as one model with two names; the point is that
    // ours asks for an id the provider does not have.
    const ours = screen.getByRole("link", { name: "retired-model" });
    // And it links to the model, which is where it gets repointed or retired.
    expect(ours).toHaveAttribute("href", "/admin/models/m-retired");
    expect(ours.parentElement).toHaveTextContent("provider/retired");
    // The direction is carried by the arrow, so the row itself stays one line.
    expect(ours.parentElement).toHaveTextContent("→");
  });

  it("says when a dropped model is already deactivated", async () => {
    // The urgency differs: a deactivated model cannot be called, so this is a
    // tidy-up rather than a request waiting to fail.
    const user = userEvent.setup();
    const discovery: CatalogueDiscovery = {
      ...DISCOVERY,
      missing_upstream: [
        { id: "m-off", name: "old-model", upstream_model: "provider/gone", is_active: false },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/providers"))
          return jsonResponse([
            {
              id: "pr1",
              name: "acme",
              description: null,
              base_url: "https://acme.test/v1",
              api_key_hint: "sk-a…3456",
              has_api_key: true,
              extra_headers: {},
              is_active: true,
              model_count: 1,
              created_at: "2026-08-01T10:00:00Z",
              updated_at: "2026-08-01T10:00:00Z",
            },
          ]);
        if (url.includes("/models/discover")) return jsonResponse(discovery);
        if (url.includes("/api/admin/models")) return jsonResponse([model()]);
        return jsonResponse([]);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);

    expect(await screen.findByText("inactive")).toBeInTheDocument();
  });

  it("does not flag a model as withdrawn just because a different tag is selected", async () => {
    // The gateway bug this pins: `missing_upstream` used to be judged against
    // the tag-scoped fetch, so an embedding model this deployment serves
    // turned into a false "no longer offered upstream" the moment an operator
    // picked `tag=Instruct`. The fix moved that judgement to the whole
    // catalogue; this only re-checks that the console renders whichever split
    // the server sends rather than deriving its own from `available` (which
    // stays tag-scoped) — a model reported `catalogued` must never show here,
    // even while a tag other than its own is selected.
    const user = userEvent.setup();
    const discoveryUnderATag: CatalogueDiscovery = {
      ...DISCOVERY,
      catalogued: [
        {
          id: "m-embed",
          name: "our-embedder",
          upstream_model: "provider/embed-1",
          is_active: true,
        },
      ],
      missing_upstream: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/models/tags")) return jsonResponse(TAGS);
        if (url.includes("/models/discover")) return jsonResponse(discoveryUnderATag);
        if (url.includes("/api/admin/providers"))
          return jsonResponse([
            {
              id: "pr1",
              name: "acme",
              description: null,
              base_url: "https://acme.test/v1",
              api_key_hint: "sk-a…3456",
              has_api_key: true,
              extra_headers: {},
              is_active: true,
              model_count: 1,
              created_at: "2026-08-01T10:00:00Z",
              updated_at: "2026-08-01T10:00:00Z",
            },
          ]);
        if (url.includes("/api/admin/models")) return jsonResponse([model()]);
        return jsonResponse([]);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Catalogue tag" }),
      "Instruct",
    );

    await waitFor(() => expect(screen.getByText("new-1")).toBeInTheDocument());
    expect(screen.queryByText(/no longer offered upstream/i)).not.toBeInTheDocument();
  });

  it("imports a model priced in another currency", async () => {
    // ADR 0054: USD prices come along as published, and conversion happens
    // at admission using the day's rate.
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    const checkbox = await screen.findByLabelText("Import dollar-1");
    expect(checkbox).toBeEnabled();
    expect(screen.queryByText(/cannot import/i)).not.toBeInTheDocument();
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

  it("offers the provider's tag words as a dropdown, read live from them", async () => {
    // The vocabulary is the counterparty's, and free text assumed the operator
    // already knew it — the same blindness that hid the embedding and OCR
    // models behind Cortecs' Instruct default. Whatever the provider answers
    // an "everything" fetch with is what the dropdown offers.
    const user = userEvent.setup();
    const calls: { discoverUrls?: string[]; tagUrls?: string[] } = {};
    vi.stubGlobal("fetch", routes([model()], calls, undefined, TAGS));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);

    const tagSelect = await screen.findByRole("combobox", { name: "Catalogue tag" });
    expect(
      within(tagSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Provider default", "Embedding", "Instruct", "OCR"]);
    expect(calls.tagUrls?.[0]).toContain("provider_id=pr1");

    // Asking for a slice re-asks the provider with that slice named.
    await user.selectOptions(tagSelect, "Embedding");
    await waitFor(() =>
      expect(calls.discoverUrls?.some((url) => url.includes("tag=Embedding"))).toBe(true),
    );
  });

  it("imports under the tag the candidates were read with", async () => {
    // With the wrong tag the model would not be in the catalogue the import
    // reads at all — the same question, so the same answer travels with it.
    const user = userEvent.setup();
    const calls: { importUrls?: string[] } = {};
    vi.stubGlobal("fetch", routes([model()], calls, undefined, TAGS));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.selectOptions(await screen.findByRole("combobox", { name: "Catalogue tag" }), "OCR");
    await user.click(await screen.findByLabelText("Import new-1"));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: /^Import/ }),
    );

    await waitFor(() => expect(calls.importUrls?.[0]).toContain("tag=OCR"));
  });

  it("keeps free text for a provider with no known tag vocabulary", async () => {
    // A dropdown of one default option would be a worse version of the box it
    // replaced, and pretend the vocabulary it does not have. The default mock
    // answers unsupported, which is every generic endpoint's case.
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);

    expect(await screen.findByRole("textbox", { name: "Catalogue tag" })).toBeInTheDocument();
  });

  it("asks for the provider before anything else when adding a model by hand", async () => {    // Position, not existence: a model is routed through one endpoint
    // (ADR 0027), so the provider is the decision the rest of the form hangs
    // on. It sat below fields its answer constrains, and read as an
    // afterthought — the order here is what keeps it first.
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog");

    const provider = within(dialog).getByLabelText("Provider");
    const name = within(dialog).getByLabelText("Name");
    const upstream = within(dialog).getByLabelText("Upstream model");
    expect(provider.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      provider.compareDocumentPosition(upstream) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("leaves the deployment's own plumbing out of the provider pickers", async () => {
    // The extractor's row is infrastructure with a fixed builtin catalogue —
    // the model list already hides its rows, and offering it in the pickers
    // would re-present the same plumbing as a choice one screen later. Search
    // backends stay out for the older reason (ADR 0071: they have their own
    // screen); both pickers share the verdict.
    const user = userEvent.setup();
    const base = routes([model()]);
    const row = (id: string, name: string, kind: string) => ({
      id,
      name,
      description: null,
      base_url: `https://${id}.test/v1`,
      api_key_hint: null,
      has_api_key: false,
      extra_headers: {},
      is_active: true,
      kind,
      model_count: 1,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/api/admin/providers")) {
          return jsonResponse([
            row("pr1", "acme", "provider"),
            row("prx", "extractor", "internal"),
            row("prs", "exa", "search"),
          ]);
        }
        return base(input, init);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Add model" }));
    const createDialog = await screen.findByRole("dialog");
    expect(
      within(within(createDialog).getByLabelText("Provider"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose an endpoint…", "acme — https://pr1.test/v1"]);
    await user.click(within(createDialog).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Discover" }));
    const discoveryDialog = await screen.findByRole("dialog");
    expect(
      within(within(discoveryDialog).getByLabelText("Provider"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Choose an endpoint…", "acme"]);
  });
});
