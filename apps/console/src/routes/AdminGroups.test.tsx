import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SeenGroup } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminGroups } from "./AdminGroups";

/**
 * The "Groups from your identity provider" section: what an operator reads to
 * decide which of a directory's names mean something here. Tested for what it
 * says (the mode, who carries what, the empty state) and that its buttons
 * call the right routes.
 */

function seen(overrides: Partial<SeenGroup> = {}): SeenGroup {
  return {
    id: "s1",
    name: "gitlab/eng",
    issuer: "https://gitlab.example.org",
    provider: "default",
    people: 12,
    first_seen_at: "2026-10-01T09:00:00Z",
    last_seen_at: "2026-10-08T09:00:00Z",
    dismissed: false,
    ...overrides,
  };
}

function routes(items: SeenGroup[], mode: "manual" | "auto" = "manual") {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.includes("/api/admin/groups/seen/") && method === "POST") {
      if (url.endsWith("/import"))
        return jsonResponse({
          group: { id: "g9", name: "gitlab/eng", description: null, source: "oidc", is_active: true, member_count: 12, models: [],
            aliases: [], search_backend: null },
          members_added: 12,
          applied: "now",
        });
      if (url.endsWith("/map"))
        return jsonResponse({
          group: { id: "g2", name: "engineering", description: null, source: "manual", is_active: true, member_count: 3, models: [],
            aliases: [], search_backend: null },
          members_added: 2,
          applied: "now",
        });
      return new Response(null, { status: 204 });
    }
    if (url.includes("/api/admin/groups/seen"))
      return jsonResponse({
        items,
        total: items.length,
        limit: 50,
        offset: 0,
        group_import: mode,
        default_group: "users",
      });
    if (url.includes("/api/admin/oidc/policy") && method === "PUT")
      return jsonResponse({ group_mappings: [] });
    if (url.includes("/api/admin/oidc/policy"))
      return jsonResponse({
        auto_provision: true,
        unknown_user_policy: "refuse",
        join_mode: "open",
        required_groups: [],
        group_fallback: "refuse",
        groups_claim: "groups",
        group_mappings: [
          { idp: "gitlab/old", local: "engineering" },
        ],
        source: "console",
        sources: { group_mappings: "console" },
        configured: null,
        propagation_seconds: 10,
      });
    if (method === "PATCH" && /\/api\/admin\/groups\/[^/]+$/.test(url)) {
      const payload = JSON.parse(String(init?.body ?? "{}")) as {
        code_agents_allowed?: boolean;
        api_keys_allowed?: boolean;
      };
      return jsonResponse({
        id: "g2",
        name: "engineering",
        description: null,
        source: "manual",
        is_active: true,
        code_agents_allowed: payload.code_agents_allowed ?? false,
        api_keys_allowed: payload.api_keys_allowed ?? true,
        member_count: 1,
        models: [],
            aliases: [],
        search_backend: null,
      });
    }
    if (url.includes("/api/admin/groups"))
      return jsonResponse({
        items: [
          { id: "g2", name: "engineering", description: null, source: "manual", is_active: true, code_agents_allowed: false, api_keys_allowed: true, member_count: 1, models: [],
            aliases: [], search_backend: null },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      });
    return jsonResponse([]);
  });
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminGroups />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AdminGroups — seen from your identity provider", () => {
  it("lists the names with how many people carry each, and explains the mode", async () => {
    vi.stubGlobal("fetch", routes([seen(), seen({ id: "s2", name: "gitlab/ops", people: 1 })]));
    renderScreen();

    const section = (await screen.findByText("Groups from your identity provider")).closest("section")!;
    await waitFor(() => expect(within(section).getByText("gitlab/eng")).toBeInTheDocument());
    expect(within(section).getByText("12")).toBeInTheDocument();
    expect(within(section).getByText(/Nothing is created until you map it/)).toBeInTheDocument();
    expect(within(section).getByText("users")).toBeInTheDocument();
  });

  it("says when names are mapped automatically", async () => {
    vi.stubGlobal("fetch", routes([], "auto"));
    renderScreen();
    expect(await screen.findByText(/Every name is mapped automatically at sign-in/)).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen();
    expect(await screen.findByText("Nothing waiting to be mapped.")).toBeInTheDocument();
  });

  it("offers no Import anywhere — mapping is the one decision", async () => {
    vi.stubGlobal("fetch", routes([seen()]));
    renderScreen();
    await screen.findByText("gitlab/eng");
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Map…" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Ignore" }).length).toBeGreaterThan(0);
  });

  it("dismisses through the seen-group route", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    // Each row carries its buttons twice (the phone layout's copy is hidden
    // by CSS, which jsdom does not apply); either one is the same action.
    await user.click((await screen.findAllByRole("button", { name: "Ignore" }))[0]!);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/groups/seen/s1/dismiss"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("searches on the server and can show the dismissed ones", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    const section = (await screen.findByText("Groups from your identity provider")).closest("section")!;
    await user.type(within(section).getByLabelText("Search"), "ops");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/admin\/groups\/seen\?.*q=ops/),
        expect.anything(),
      ),
    );

    await user.click(within(section).getByRole("button", { name: "Show dismissed" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/admin\/groups\/seen\?.*dismissed=true/),
        expect.anything(),
      ),
    );
  });
});

describe("SeenGroups: the Map dialog's three paths", () => {
  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getAllByRole("button", { name: "Map…" })[0]!);
    return screen.getByRole("dialog");
  }

  it("defaults to a new group with the same name, and maps through the map route", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    await screen.findByText("gitlab/eng");
    // The policy's existing mappings render with their Unmap.
    expect(await screen.findByText("gitlab/old")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unmap gitlab/old" })).toBeInTheDocument();

    const dialog = await openDialog(user);
    expect(
      within(dialog).getByRole("radio", { name: /New group with the same name/ }),
    ).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/groups/seen/s1/map"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const call = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/seen/s1/map"),
    );
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      target: "new_same_name",
      reason: "",
    });
  });

  it("creates a group under another name when that is chosen", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    await screen.findByText("gitlab/eng");
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("radio", { name: /New group named/ }));
    await user.type(within(dialog).getByLabelText(/Name the new group/), "engineering");
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/seen/s1/map"));
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      target: "new_named",
      name: "engineering",
      reason: "",
    });
  });

  it("maps onto an existing group through the picker when that is chosen", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    await screen.findByText("gitlab/eng");
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("radio", { name: /Existing group/ }));
    await user.selectOptions(within(dialog).getByLabelText(/Local group/), "g2");
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/seen/s1/map"));
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      target: "existing",
      group_id: "g2",
      reason: "",
    });
  });

  it("unmaps through the policy PUT, dropping only that pair", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    await user.click(await screen.findByRole("button", { name: "Unmap gitlab/old" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/oidc/policy"),
        expect.objectContaining({ method: "PUT" }),
      ),
    );
    const call = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes("/oidc/policy") && (init as RequestInit).method === "PUT",
    );
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      group_mappings: [],
      reason: "Unmapped gitlab/old",
    });
  });
});

describe("AdminGroups — capability flags (ADR 0096)", () => {
  it("shows the two toggles per group, seeded from the listing", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen();

    const agents = await screen.findByRole("checkbox", { name: "engineering: coding agents" });
    const keys = screen.getByRole("checkbox", { name: "engineering: API keys" });
    // The listing seeds the mock: agents off, keys on.
    expect(agents).not.toBeChecked();
    expect(keys).toBeChecked();
  });

  it("flips a flag through the group PATCH route", async () => {
    const fetchMock = routes([]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    await user.click(
      await screen.findByRole("checkbox", { name: "engineering: coding agents" }),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/groups/g2"),
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
    const call = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/admin/groups/g2") && (init as RequestInit).method === "PATCH",
    );
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      code_agents_allowed: true,
    });
  });
});
