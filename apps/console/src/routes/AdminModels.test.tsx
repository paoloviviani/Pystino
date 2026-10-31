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
      per_minute: null,
      per_search: null,
      cache_read_per_mtok: null,
      cache_write_per_mtok: null,
      per_image: null,
      per_image_by_size: null,
      currency: "EUR",
      effective_from: "2026-08-01T10:00:00Z",
      source: "manual",
    },
    input_modalities: ["text"],
    output_modalities: ["text"],
    supported_features: [],
    granted_to: ["research"],
    granted_to_users: [],
    aliases_pointing_here: [],
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
      per_minute: null,
      currency: "EUR",
      context_window: 32000,
      kind: "chat",
      input_modalities: ["text", "image"],
      output_modalities: ["text"],
      supported_features: ["tools"],
      blocked_reason: null,
      price_source: "provider",
      max_input_tokens: null,
      max_output_tokens: null,
    },
    {
      upstream_model: "provider/dollar-1",
      suggested_name: "dollar-1",
      input_per_mtok: "0.900000000000",
      output_per_mtok: "1.900000000000",
      per_page: null,
      per_minute: null,
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
      max_input_tokens: null,
      max_output_tokens: null,
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
      per_minute: null,
      currency: "USD",
      context_window: 8000,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: null,
      price_source: "community",
      max_input_tokens: null,
      max_output_tokens: null,
    },
    {
      upstream_model: "provider/nowhere-1",
      suggested_name: "nowhere-1",
      input_per_mtok: null,
      output_per_mtok: null,
      per_page: null,
      per_minute: null,
      currency: null,
      context_window: null,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: "the provider publishes no price for this model",
      price_source: null,
      max_input_tokens: null,
      max_output_tokens: null,
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
    /** The import bodies as they were sent, for tests that assert the payload. */
    importBodies?: { models: { upstream_model: string; manual_price?: unknown }[] }[];
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
      const body = JSON.parse(String(init?.body)) as {
        models: { upstream_model: string; manual_price?: unknown }[];
      };
      (calls.importBodies ??= []).push(body);
      calls.imported = body.models.map((entry) => entry.upstream_model);
      payload = {
        results: body.models.map((entry) => ({
          upstream_model: entry.upstream_model,
          name: "new-1",
          imported: true,
          priced: true,
          reason: null,
          // An entry that carried a price typed by hand is answered as one;
          // the older tests send no manual_price and are unaffected.
          price_source:
            entry.manual_price !== undefined
              ? "manual"
              : url.includes("fill_missing_prices=true")
                ? "community"
                : "provider",
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
  it("shows the display name a caller's picker will show, with the id beside it", async () => {
    vi.stubGlobal(
      "fetch",
      routes([model({ display_name: "Fast summariser" })]),
    );
    renderScreen(<AdminModels />);

    // The catalogue answers both questions: the display name is what people
    // read, the id is what a request has to carry — and a renamed model whose
    // id is invisible is how an operator ends up sending the wrong one.
    await waitFor(() => expect(screen.getByText("Fast summariser")).toBeInTheDocument());
    expect(screen.getByText("fast-summariser")).toBeInTheDocument();
  });

  it("shows no display-name line for a model that has none", async () => {
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("fast-summariser")).toBeInTheDocument());
    // Nothing to show, so nothing shown — not the id repeated as its own name.
    expect(screen.queryByText("Unnamed")).not.toBeInTheDocument();
  });

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

  it("gives transcription models their own section, with the route that serves them", async () => {
    vi.stubGlobal(
      "fetch",
      routes([model(), model({ id: "m5", name: "whisper", kind: "transcription" })]),
    );
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("whisper")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /^Transcription/ })).toBeInTheDocument();
    expect(screen.getByText(/\/v1\/audio\/transcriptions/)).toBeInTheDocument();
  });

  it("gives decision models their own section, with the routes that serve them", async () => {
    vi.stubGlobal("fetch", routes([model(), model({ id: "m4", name: "triage", kind: "decision" })]));
    renderScreen(<AdminModels />);

    await waitFor(() => expect(screen.getByText("triage")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /^Decision/ })).toBeInTheDocument();
    expect(screen.getByText(/\/v1\/decisions and \/v1\/systemone/)).toBeInTheDocument();
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
    // And a model nobody prices can now be selected: a price entered on the
    // row itself is what makes it importable, which is the whole point of the
    // fields this dialog offers (this used to pin the opposite).
    expect(screen.getByRole("checkbox", { name: /Import nowhere-1/i })).toBeEnabled();
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

  it("pre-ticks Images in tool results when a hand-made model accepts images", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog");
    const features = within(within(dialog).getByRole("group", { name: "Features" }));
    const option = () => features.getByRole("checkbox", { name: /^Images in tool results/ });

    // Only meaningful with image input, so it waits for it.
    expect(option()).toBeDisabled();
    expect(option()).not.toBeChecked();

    const accepts = within(within(dialog).getByRole("group", { name: "Accepts" }));
    await user.click(accepts.getByRole("checkbox", { name: /^image/ }));
    expect(option()).toBeEnabled();
    expect(option()).toBeChecked();
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

  it("offers a price on a ticked unpriced row, and imports it by hand", async () => {
    // The provider lists the model and prices nothing; until now the only way
    // in was one form per model. The price is typed on the row itself, sent
    // with the import, and the answer says who supplied it.
    const user = userEvent.setup();
    const calls: {
      imported?: string[];
      importUrls?: string[];
      importBodies?: { models: { upstream_model: string; manual_price?: unknown }[] }[];
    } = {};
    vi.stubGlobal("fetch", routes([model()], calls));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));
    await waitFor(() => expect(screen.getByText("nowhere-1")).toBeInTheDocument());

    // Nothing to type until the row is chosen: the fields belong to a row, not
    // to the dialog.
    expect(screen.queryByLabelText("Input per Mtok")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Import nowhere-1" }));
    await user.type(await screen.findByLabelText("Input per Mtok"), "1.5");
    await user.type(screen.getByLabelText("Output per Mtok"), "6");

    await user.click(screen.getByRole("button", { name: /^Import/ }));
    await waitFor(() => expect(calls.imported).toEqual(["provider/nowhere-1"]));
    expect(calls.importBodies).toHaveLength(1);
    expect(calls.importBodies?.[0]?.models[0]?.manual_price).toEqual({
      input_per_mtok: "1.5",
      output_per_mtok: "6",
    });
    expect(await screen.findByText(/with the price entered by hand/i)).toBeInTheDocument();
  });

  it("does not import an unpriced row whose price is missing or malformed", async () => {
    // An empty price would import the model unpriced, which is the refusal the
    // server keeps; a malformed one would be refused there anyway. Both are
    // stopped at the button, with the reason under the field.
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));
    await waitFor(() => expect(screen.getByText("nowhere-1")).toBeInTheDocument());
    await user.click(screen.getByRole("checkbox", { name: "Import nowhere-1" }));

    const importButton = await screen.findByRole("button", { name: /^Import/ });
    await waitFor(() => expect(importButton).toBeDisabled());

    const input = screen.getByLabelText("Input per Mtok");
    await user.type(input, "1,5");
    expect(importButton).toBeDisabled();
    expect(screen.getByText(/digits and an optional decimal point/i)).toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "1.5");
    await user.type(screen.getByLabelText("Output per Mtok"), "6");
    await waitFor(() => expect(importButton).toBeEnabled());
  });

  it("asks the currency only when one is typed, and then only three letters", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([model()]));
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await user.click(screen.getByRole("checkbox", { name: /fill missing prices/i }));
    await waitFor(() => expect(screen.getByText("nowhere-1")).toBeInTheDocument());
    await user.click(screen.getByRole("checkbox", { name: "Import nowhere-1" }));

    const importButton = await screen.findByRole("button", { name: /^Import/ });
    const currency = screen.getByLabelText("Currency");
    // Empty means the gateway's billing currency — the same default the model
    // page's price form sends — so an empty field is a valid one.
    expect(currency).toHaveValue("");
    await user.type(currency, "EURO");
    await user.type(screen.getByLabelText("Input per Mtok"), "1");
    await user.type(screen.getByLabelText("Output per Mtok"), "2");
    await waitFor(() => expect(importButton).toBeDisabled());

    await user.clear(currency);
    await user.type(currency, "EUR");
    await waitFor(() => expect(importButton).toBeEnabled());
  });

  it("asks each kind for the rates it is metered on", async () => {
    // A chat model bills output tokens too, an embedding model generates none,
    // an OCR model charges by the page, and a search tier is metered per
    // request — the same rule the server enforces, mirrored so the row says
    // what it needs before the import is refused for saying something else.
    const user = userEvent.setup();
    const base = routes([model()]);
    const unpriced = (upstream: string, kind: string) => ({
      upstream_model: upstream,
      suggested_name: upstream.replace("provider/", ""),
      input_per_mtok: null,
      output_per_mtok: null,
      per_page: null,
      per_minute: null,
      currency: null,
      context_window: null,
      kind,
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: "the provider publishes no price: enter one to import it",
      price_source: null,
      max_input_tokens: null,
      max_output_tokens: null,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/models/discover")) {
          return jsonResponse({
            ...DISCOVERY_FILLED,
            available: [
              unpriced("provider/plain-chat", "chat"),
              unpriced("provider/the-embedder", "embedding"),
              unpriced("provider/the-reader", "ocr"),
              unpriced("provider/the-finder", "search"),
            ],
          });
        }
        return base(input, init);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await waitFor(() => expect(screen.getByText("plain-chat")).toBeInTheDocument());

    for (const name of ["plain-chat", "the-embedder", "the-reader", "the-finder"]) {
      await user.click(screen.getByRole("checkbox", { name: `Import ${name}` }));
    }

    // The chat row wants both token rates; the embedding row only an input
    // rate; the OCR row only a page rate.
    expect(screen.getAllByLabelText("Input per Mtok")).toHaveLength(2);
    expect(screen.getByLabelText("Output per Mtok")).toBeInTheDocument();
    expect(screen.getByLabelText("Per page")).toBeInTheDocument();
    // And the search tier wants nothing at all — it was never priced per token.
    expect(screen.getAllByLabelText("Currency")).toHaveLength(3);
    const searchTable = screen.getByRole("table", { name: /search tiers/i });
    expect(within(searchTable).queryAllByRole("textbox")).toHaveLength(0);
  });

  it("copies the typed price to every other ticked unpriced row", async () => {
    // A provider with many models at one rate would otherwise mean retyping
    // the same three figures per row.
    const user = userEvent.setup();
    const calls: {
      importBodies?: { models: { upstream_model: string; manual_price?: unknown }[] }[];
    } = {};
    const base = routes([model()], calls);
    const unpriced = (upstream: string) => ({
      upstream_model: upstream,
      suggested_name: upstream.replace("provider/", ""),
      input_per_mtok: null,
      output_per_mtok: null,
      per_page: null,
      per_minute: null,
      currency: null,
      context_window: null,
      kind: "chat",
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: "the provider publishes no price: enter one to import it",
      price_source: null,
      max_input_tokens: null,
      max_output_tokens: null,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/models/discover")) {
          return jsonResponse({
            ...DISCOVERY_FILLED,
            available: [unpriced("provider/cheap-a"), unpriced("provider/cheap-b")],
          });
        }
        return base(input, init);
      }),
    );
    renderScreen(<AdminModels />);

    await user.click(screen.getByRole("button", { name: "Discover" }));
    await openCatalogue(user);
    await waitFor(() => expect(screen.getByText("cheap-a")).toBeInTheDocument());
    await user.click(screen.getByRole("checkbox", { name: "Import cheap-a" }));
    await user.click(screen.getByRole("checkbox", { name: "Import cheap-b" }));

    const firstInput = (await screen.findAllByLabelText("Input per Mtok"))[0]!;
    await user.type(firstInput, "0.5");
    await user.type(screen.getAllByLabelText("Output per Mtok")[0]!, "2");
    await user.click(
      await screen.findByRole("button", { name: /apply this price to all ticked/i }),
    );

    // Both rows carry the same figures now, and both go out priced by hand.
    const inputs = screen.getAllByLabelText("Input per Mtok");
    expect(inputs).toHaveLength(2);
    expect(inputs[1]).toHaveValue("0.5");
    expect(screen.getAllByLabelText("Output per Mtok")[1]).toHaveValue("2");

    await user.click(screen.getByRole("button", { name: /^Import/ }));
    await waitFor(() => expect(calls.importBodies).toHaveLength(1));
    expect(calls.importBodies?.[0]?.models).toEqual([
      {
        upstream_model: "provider/cheap-a",
        manual_price: { input_per_mtok: "0.5", output_per_mtok: "2" },
      },
      {
        upstream_model: "provider/cheap-b",
        manual_price: { input_per_mtok: "0.5", output_per_mtok: "2" },
      },
    ]);
  });

  describe("cache rates on a hand-typed price", () => {
    const unpriced = (upstream: string, kind = "chat") => ({
      upstream_model: upstream,
      suggested_name: upstream.replace("provider/", ""),
      input_per_mtok: null,
      output_per_mtok: null,
      per_page: null,
      per_minute: null,
      currency: null,
      context_window: null,
      kind,
      input_modalities: [],
      output_modalities: [],
      supported_features: [],
      blocked_reason: "the provider publishes no price: enter one to import it",
      price_source: null,
      max_input_tokens: null,
      max_output_tokens: null,
    });
    const setup = async (rows: ReturnType<typeof unpriced>[]) => {
      const user = userEvent.setup();
      const calls: {
        importBodies?: { models: { upstream_model: string; manual_price?: unknown }[] }[];
      } = {};
      const base = routes([model()], calls);
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          if (String(input).includes("/models/discover")) {
            return jsonResponse({ ...DISCOVERY_FILLED, available: rows });
          }
          return base(input, init);
        }),
      );
      renderScreen(<AdminModels />);
      await user.click(screen.getByRole("button", { name: "Discover" }));
      await openCatalogue(user);
      await waitFor(() => expect(screen.getByText(rows[0]!.suggested_name)).toBeInTheDocument());
      return { user, calls };
    };

    it("offers optional cache fields on ticked chat and embedding rows, not OCR, and sends what is typed", async () => {
      const { user, calls } = await setup([
        unpriced("provider/chatty"),
        unpriced("provider/embedder", "embedding"),
        unpriced("provider/reader", "ocr"),
      ]);
      await user.click(screen.getByRole("checkbox", { name: "Import chatty" }));
      await user.click(
        screen.getByRole("checkbox", { name: "Import embedder" }),
      );
      await user.click(screen.getByRole("checkbox", { name: "Import reader" }));
      // Chat and embedding: a pair of cache fields each. OCR is priced per page: none.
      expect(screen.getAllByLabelText("Cache read per Mtok")).toHaveLength(2);
      expect(screen.getAllByLabelText("Cache write per Mtok")).toHaveLength(2);
      expect(
        screen.getAllByText(
          "Leave empty to bill cached tokens at the input rate.",
        ),
      ).toHaveLength(2);

      const inputs = screen.getAllByLabelText("Input per Mtok");
      await user.type(inputs[0]!, "1.5");
      await user.type(screen.getByLabelText("Output per Mtok"), "6");
      await user.type(inputs[1]!, "0.1");
      await user.type(
        screen.getAllByLabelText("Cache read per Mtok")[0]!,
        "0.15",
      );
      await user.type(
        screen.getAllByLabelText("Cache write per Mtok")[0]!,
        "1.875",
      );
      await user.type(
        screen.getAllByLabelText("Cache read per Mtok")[1]!,
        "0.01",
      );
      await user.type(screen.getByLabelText("Per page"), "0.002");
      await user.click(screen.getByRole("button", { name: /^Import/ }));
      await waitFor(() => expect(calls.importBodies).toHaveLength(1));
      const bodies = calls.importBodies![0]!.models;
      expect(
        bodies.find((x) => x.upstream_model === "provider/chatty")
          ?.manual_price,
      ).toEqual({
        input_per_mtok: "1.5",
        output_per_mtok: "6",
        cache_read_per_mtok: "0.15",
        cache_write_per_mtok: "1.875",
      });
      expect(
        bodies.find((x) => x.upstream_model === "provider/embedder")
          ?.manual_price,
      ).toEqual({
        input_per_mtok: "0.1",
        cache_read_per_mtok: "0.01",
      });
      expect(
        bodies.find((x) => x.upstream_model === "provider/reader")
          ?.manual_price,
      ).toEqual({
        per_page: "0.002",
      });
    });

    it("asks a transcription row for a rate per minute of audio and sends it", async () => {
      const { user, calls } = await setup([
        unpriced("provider/whisper-large", "transcription"),
      ]);
      await user.click(screen.getByRole("checkbox", { name: "Import whisper-large" }));
      // Priced per minute: no token rates, no cache fields, no page rate.
      expect(screen.queryByLabelText("Input per Mtok")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Per page")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Cache read per Mtok")).not.toBeInTheDocument();
      await user.type(screen.getByLabelText("Per minute of audio"), "0.006");
      await user.click(screen.getByRole("button", { name: /^Import/ }));
      await waitFor(() => expect(calls.importBodies).toHaveLength(1));
      expect(calls.importBodies![0]!.models[0]?.manual_price).toEqual({
        per_minute: "0.006",
      });
    });

    it("omits an empty cache field, and blocks a malformed one", async () => {
      const { user, calls } = await setup([unpriced("provider/chatty")]);
      await user.click(screen.getByRole("checkbox", { name: "Import chatty" }));
      await user.type(await screen.findByLabelText("Input per Mtok"), "1");
      await user.type(screen.getByLabelText("Output per Mtok"), "2");
      await user.type(screen.getByLabelText("Cache read per Mtok"), "abc");
      expect(screen.getByRole("button", { name: /^Import/ })).toBeDisabled();
      await user.clear(screen.getByLabelText("Cache read per Mtok"));
      await user.click(screen.getByRole("button", { name: /^Import/ }));
      await waitFor(() => expect(calls.importBodies).toHaveLength(1));
      expect(calls.importBodies![0]!.models[0]!.manual_price).toEqual({
        input_per_mtok: "1",
        output_per_mtok: "2",
      });
    });

    it("copies the cache rates with the rest of the price", async () => {
      const { user, calls } = await setup([
        unpriced("provider/cheap-a"),
        unpriced("provider/cheap-b"),
      ]);
      await user.click(screen.getByRole("checkbox", { name: "Import cheap-a" }));
      await user.click(screen.getByRole("checkbox", { name: "Import cheap-b" }));
      await user.type((await screen.findAllByLabelText("Input per Mtok"))[0]!, "0.5");
      await user.type(screen.getAllByLabelText("Output per Mtok")[0]!, "2");
      await user.type(screen.getAllByLabelText("Cache read per Mtok")[0]!, "0.05");
      await user.click(
        await screen.findByRole("button", { name: /apply this price to all ticked/i }),
      );
      expect(screen.getAllByLabelText("Cache read per Mtok")[1]).toHaveValue("0.05");
      await user.click(screen.getByRole("button", { name: /^Import/ }));
      await waitFor(() => expect(calls.importBodies).toHaveLength(1));
      for (const entry of calls.importBodies![0]!.models) {
        expect(entry.manual_price).toEqual({
          input_per_mtok: "0.5",
          output_per_mtok: "2",
          cache_read_per_mtok: "0.05",
        });
      }
    });
  });
});
