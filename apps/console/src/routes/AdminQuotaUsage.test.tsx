import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MoneyPrecisionProvider } from "@llmp/ui";
import type { UserQuotaEntry, UserQuotaSummary } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminQuotaUsage } from "./AdminQuotaUsage";

/**
 * The page is read to decide whether to reset someone, so what is pinned is the
 * request that reset sends (it names the user), that the figures refresh after
 * it, and that a shared quota offers no per-user reset at all.
 */

function entry(overrides: Partial<UserQuotaEntry> = {}): UserQuotaEntry {
  return {
    rule_id: "r-member",
    rule_name: "per member",
    kind: "each_member",
    scope: "group",
    scope_label: "research",
    metric: "requests",
    limit_value: "4",
    current_value: "3",
    percent: 75,
    window_label: "month",
    window_seconds: null,
    period: "month",
    resets_at: "2026-11-01T00:00:00Z",
    resets_at_is_estimate: false,
    last_reset_at: null,
    last_reset_by_email: null,
    last_reset_for_user: false,
    can_reset_for_user: true,
    replaced_by_rule_id: null,
    replaced_by_name: null,
    ...overrides,
  };
}

const SHARED = entry({
  rule_id: "r-shared",
  rule_name: "group total",
  kind: "shared",
  limit_value: "50",
  current_value: "12",
  percent: 24,
  can_reset_for_user: false,
});
const REPLACED = entry({
  rule_id: "r-replaced",
  rule_name: "each user requests",
  kind: "each_user",
  scope: "global",
  scope_label: null,
  current_value: null,
  percent: null,
  can_reset_for_user: false,
  replaced_by_rule_id: "r-own",
  replaced_by_name: "my requests",
});

const ROLLING = entry({
  rule_id: "r-rolling",
  rule_name: "tokens per day",
  kind: "shared",
  scope: "global",
  scope_label: null,
  period: null,
  window_seconds: 86400,
  window_label: "1 day",
  resets_at: "2026-10-29T16:00:00Z",
  resets_at_is_estimate: true,
  current_value: "7",
  limit_value: "9",
  can_reset_for_user: false,
});
const OWN = entry({
  rule_id: "r-own",
  rule_name: "my requests",
  kind: "own",
  scope: "user",
  scope_label: null,
  current_value: "1",
  percent: 25,
  can_reset_for_user: false,
});

function summary(overrides: Partial<UserQuotaSummary> = {}): UserQuotaSummary {
  return {
    user_id: "u1",
    email: "ada@example.org",
    display_name: "Ada Lovelace",
    is_active: true,
    rules: 2,
    most_constrained: entry(),
    ...overrides,
  };
}

function limit(overrides: Record<string, unknown>) {
  return {
    id: "r",
    name: "rule",
    scope: "global",
    scope_id: null,
    applies_to: "aggregate",
    metric: "requests",
    limit_value: "50",
    current_value: "1",
    window_label: "month",
    window_seconds: null,
    period: "month",
    is_active: true,
    overrides_each_user: false,
    last_reset_at: null,
    ...overrides,
  };
}

interface Server {
  fetch: ReturnType<typeof vi.fn>;
  resetBodies: unknown[];
}

function serve(): Server {
  const resetBodies: unknown[] = [];
  let used = "3";
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/admin/limits/") && url.endsWith("/reset") && init?.method === "POST") {
      resetBodies.push(JSON.parse(String(init.body)));
      used = "0";
      return jsonResponse({ id: "x", rule_id: "r-member", effective_at: "2026-10-28T00:00:00Z", reason: "", created_by: null });
    }
    if (url.includes("/api/admin/quota-usage/u1")) {
      return jsonResponse({
        user_id: "u1",
        email: "ada@example.org",
        display_name: "Ada Lovelace",
        entries: [entry({ current_value: used }), SHARED, REPLACED, ROLLING, OWN],
      });
    }
    if (url.includes("/api/admin/quota-usage")) {
      return jsonResponse([summary(), summary({ user_id: "u2", email: "bob@example.org", display_name: null, rules: 0, most_constrained: null })]);
    }
    if (url.includes("/api/admin/limits")) {
      return jsonResponse({
        items: [
          limit({ id: "r-shared", name: "group total", scope: "group", scope_id: "g1" }),
          limit({ id: "r-global", name: "everyone total", scope: "global" }),
          limit({ id: "r-member", name: "per member", scope: "group", scope_id: "g1", applies_to: "each_member" }),
        ],
        total: 3,
        limit: 200,
        offset: 0,
      });
    }
    if (url.includes("/api/admin/groups")) {
      return jsonResponse({
        items: [{ id: "g1", name: "research", description: null }],
        total: 1,
        limit: 200,
        offset: 0,
      });
    }
    return jsonResponse([]);
  });
  return { fetch, resetBodies };
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MoneyPrecisionProvider exact={false}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/admin/quota-usage" element={<AdminQuotaUsage />} />
            <Route path="/admin/quota-usage/:userId" element={<AdminQuotaUsage />} />
          </Routes>
        </MemoryRouter>
      </MoneyPrecisionProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminQuotaUsage", () => {
  it("lists users with their most constrained quota and opens the detail", async () => {
    vi.stubGlobal("fetch", serve().fetch);
    renderAt("/admin/quota-usage");

    expect(await screen.findByText("per member")).toBeInTheDocument();
    expect(screen.getByText("3 of 4")).toBeInTheDocument();
    expect(screen.getByText("No quotas apply")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ada Lovelace" })).toHaveAttribute(
      "href",
      "/admin/quota-usage/u1",
    );
  });

  it("resets for one user: posts user_id and refreshes the value", async () => {
    const server = serve();
    vi.stubGlobal("fetch", server.fetch);
    const user = userEvent.setup();
    renderAt("/admin/quota-usage/u1");

    expect(await screen.findByText("3 of 4")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset for this user" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "extension");
    await user.click(within(dialog).getByRole("button", { name: "Reset for this user" }));

    await waitFor(() =>
      expect(server.resetBodies).toEqual([{ reason: "extension", user_id: "u1" }]),
    );
    await waitFor(() => expect(screen.getByText("0 of 4")).toBeInTheDocument());
  });

  it("offers a reset on every row kind, with the right button and no button where it is refused", async () => {
    vi.stubGlobal("fetch", serve().fetch);
    renderAt("/admin/quota-usage/u1");

    const row = async (name: string) => (await screen.findByText(name)).closest("tr")!;
    const perUser = await row("per member");
    expect(within(perUser).getByRole("button", { name: "Reset for this user" })).toBeInTheDocument();

    const own = await row("my requests");
    expect(within(own).getByRole("button", { name: "Reset this user's own quota" })).toBeInTheDocument();

    const shared = await row("group total");
    expect(within(shared).getByText("shared")).toBeInTheDocument();
    expect(within(shared).getByRole("button", { name: "Reset whole quota…" })).toBeInTheDocument();
    expect(within(shared).getByText("all members of research")).toBeInTheDocument();

    // Held to their own rule: no button, and the reason is on the row.
    const replaced = await row("each user requests");
    expect(within(replaced).getByText("Replaced by my requests")).toBeInTheDocument();
    expect(within(replaced).queryByRole("button")).toBeNull();
    expect(within(replaced).getByText(/would change nothing/)).toBeInTheDocument();
  });

  it("resets the user's own quota whole: no user_id, and says it counts only this user", async () => {
    const server = serve();
    vi.stubGlobal("fetch", server.fetch);
    const user = userEvent.setup();
    renderAt("/admin/quota-usage/u1");

    const own = (await screen.findByText("my requests")).closest("tr")!;
    await user.click(within(own).getByRole("button", { name: "Reset this user's own quota" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/counts only Ada Lovelace/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset this user's own quota" }));
    await waitFor(() => expect(server.resetBodies).toEqual([{ reason: "" }]));
  });

  it("resets a shared quota whole, naming everyone it counts", async () => {
    const server = serve();
    vi.stubGlobal("fetch", server.fetch);
    const user = userEvent.setup();
    renderAt("/admin/quota-usage/u1");

    const shared = (await screen.findByText("group total")).closest("tr")!;
    await user.click(within(shared).getByRole("button", { name: "Reset whole quota…" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/for all members of research, not only for one user/)).toBeInTheDocument();
    expect(within(dialog).getByText(/does not change|Every bill is unchanged/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset whole quota" }));
    await waitFor(() => expect(server.resetBodies).toEqual([{ reason: "" }]));
  });

  it("lists the shared quotas on the index, each resettable whole", async () => {
    const server = serve();
    vi.stubGlobal("fetch", server.fetch);
    const user = userEvent.setup();
    renderAt("/admin/quota-usage");

    const global = (await screen.findByText("everyone total")).closest("tr")!;
    expect(within(global).getByText("all users")).toBeInTheDocument();
    // A per-member rule is not shared: it is reset from the user's page.
    const card = global.closest("table")!;
    expect(within(card).queryByText("per member")).toBeNull();
    await user.click(within(global).getByRole("button", { name: "Reset whole quota…" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/for all users, not only for one user/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset whole quota" }));
    await waitFor(() => expect(server.resetBodies).toEqual([{ reason: "" }]));
  });

  it("words a rolling window's time as an estimate and a calendar one as exact", async () => {
    vi.stubGlobal("fetch", serve().fetch);
    renderAt("/admin/quota-usage/u1");

    const rolling = (await screen.findByText("tokens per day")).closest("tr")!;
    expect(within(rolling).getByText(/usage starts falling ~.*\(estimate\)/)).toBeInTheDocument();
    const calendar = screen.getByText("per member").closest("tr")!;
    expect(within(calendar).getByText(/^Resets /)).toBeInTheDocument();
    expect(within(calendar).queryByText(/estimate/)).toBeNull();
  });
});
