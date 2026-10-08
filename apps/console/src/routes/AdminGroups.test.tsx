import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SeenGroup } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminGroups } from "./AdminGroups";

/**
 * The "Seen from your identity provider" section: what an operator reads to
 * decide which of a directory's groups become groups here. Tested for what it
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
          group: { id: "g9", name: "gitlab/eng", description: null, source: "oidc", is_active: true, member_count: 12, models: [], search_backend: null },
          members_added: 12,
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
    if (url.includes("/api/admin/groups")) return jsonResponse([]);
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

    const section = (await screen.findByText("Seen from your identity provider")).closest("section")!;
    await waitFor(() => expect(within(section).getByText("gitlab/eng")).toBeInTheDocument());
    expect(within(section).getByText("12")).toBeInTheDocument();
    expect(within(section).getByText(/Nothing is created until you import it/)).toBeInTheDocument();
    expect(within(section).getByText("users")).toBeInTheDocument();
  });

  it("says when groups are imported automatically", async () => {
    vi.stubGlobal("fetch", routes([], "auto"));
    renderScreen();
    expect(await screen.findByText(/Groups are imported automatically at sign-in/)).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen();
    expect(await screen.findByText("Nothing waiting to be imported.")).toBeInTheDocument();
  });

  it("imports and dismisses through the seen-group routes", async () => {
    const fetchMock = routes([seen()]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderScreen();

    // Each row carries its buttons twice (the phone layout's copy is hidden
    // by CSS, which jsdom does not apply); either one is the same action.
    await user.click((await screen.findAllByRole("button", { name: "Import" }))[0]!);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/groups/seen/s1/import"),
        expect.objectContaining({ method: "POST" }),
      ),
    );

    await user.click(screen.getAllByRole("button", { name: "Dismiss" })[0]!);
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

    const section = (await screen.findByText("Seen from your identity provider")).closest("section")!;
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
