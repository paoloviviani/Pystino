import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import type { AdminUser } from "../lib/types";
import { AdminUsers } from "./AdminUsers";

/**
 * The users screen is where pagination actually earns its place, so what is
 * pinned here is the part that used to be a lie: the search box searches every
 * account, not the page that happens to be loaded.
 *
 * The rest are the two failure modes a paginated table invites. Searching from
 * page three must go back to page one, or the operator sees an empty table and
 * concludes there are no matches. And the range must be shown, or a truncated
 * listing is indistinguishable from a complete one.
 */

function user(index: number, overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: `u${index}`,
    email: `person-${index}@example.org`,
    display_name: `Person ${index}`,
    issuer: "https://idp.test",
    subject: `subject-${index}`,
    is_active: true,
    is_admin: false,
    groups: ["research"],
    default_billing_group: "research",
    active_key_count: 1,
    last_login_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

interface Seen {
  users: URLSearchParams[];
}

/**
 * A fake directory of `total` accounts that honours limit, offset and q, so a
 * test can assert on what came back rather than only on what was asked for.
 */
function routes(total: number, seen: Seen = { users: [] }) {
  const everyone = Array.from({ length: total }, (_, index) => user(index));
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname === "/api/admin/users") {
      seen.users.push(url.searchParams);
      const q = url.searchParams.get("q") ?? "";
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const matched = q
        ? everyone.filter((entry) => entry.email?.includes(q) || entry.subject.includes(q))
        : everyone;
      return new Response(
        JSON.stringify({
          items: matched.slice(offset, offset + limit),
          total: matched.length,
          limit,
          offset,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return jsonResponse([]);
  });
}

/**
 * The pager, scoped to its own landmark.
 *
 * `screen.getByRole("button", ...)` walks the entire document computing
 * accessible names, and the table under test has a button on every row. Over a
 * full page of users that query dominates the runtime of the whole file.
 */
function pager() {
  return within(screen.getByRole("navigation", { name: "Pagination" }));
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

function lastUsersCall(seen: Seen): URLSearchParams {
  const last = seen.users.at(-1);
  if (!last) throw new Error("the users endpoint was never called");
  return last;
}

describe("AdminUsers", () => {
  it("asks for one page, not the whole directory", async () => {
    const seen: Seen = { users: [] };
    vi.stubGlobal("fetch", routes(400, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    expect(lastUsersCall(seen).get("limit")).toBe("50");
    expect(screen.queryByText("person-60@example.org")).not.toBeInTheDocument();
  });

  it("says how many accounts there are, not how many are on screen", async () => {
    vi.stubGlobal("fetch", routes(400));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText(/of 400 users/)).toBeInTheDocument());
    expect(screen.getByText("1–50")).toBeInTheDocument();
  });

  it("sends the search to the server rather than filtering the page", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [] };
    vi.stubGlobal("fetch", routes(400, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.type(screen.getByLabelText("Search"), "person-321");

    // The match is on page seven; a client-side filter over the loaded page
    // would find nothing at all.
    await waitFor(() => expect(lastUsersCall(seen).get("q")).toBe("person-321"));
    await waitFor(() =>
      expect(screen.getByText("person-321@example.org")).toBeInTheDocument(),
    );
  });

  it("debounces rather than querying every keystroke", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [] };
    vi.stubGlobal("fetch", routes(400, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    const before = seen.users.length;
    await user_.type(screen.getByLabelText("Search"), "person-12");

    await waitFor(() => expect(lastUsersCall(seen).get("q")).toBe("person-12"));
    // Nine characters typed; one query for the settled value, not nine.
    expect(seen.users.length - before).toBeLessThan(4);
  });

  it("returns to the first page when a search is typed", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [] };
    vi.stubGlobal("fetch", routes(400, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(pager().getByRole("button", { name: "Next" }));
    await waitFor(() => expect(lastUsersCall(seen).get("offset")).toBe("50"));

    await user_.type(screen.getByLabelText("Search"), "person-3");

    // Without the reset this asks for rows 50–100 of a three-row result and
    // renders an empty table.
    await waitFor(() => expect(lastUsersCall(seen).get("q")).toBe("person-3"));
    expect(lastUsersCall(seen).get("offset")).toBe("0");
  });

  it("pages forwards and back", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [] };
    vi.stubGlobal("fetch", routes(120, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(pager().getByRole("button", { name: "Next" }));

    await waitFor(() => expect(screen.getByText("person-50@example.org")).toBeInTheDocument());
    expect(screen.getByText("51–100")).toBeInTheDocument();

    await user_.click(pager().getByRole("button", { name: "Previous" }));
    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
  });

  it("will not page past the end", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(60));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(pager().getByRole("button", { name: "Next" }));

    await waitFor(() => expect(screen.getByText("51–60")).toBeInTheDocument());
    expect(pager().getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows no pager at all when everything fits", async () => {
    vi.stubGlobal("fetch", routes(3));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
  });

  it("says a search matched nothing rather than that there are no users", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(400));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.type(screen.getByLabelText("Search"), "nobody");

    await waitFor(() => expect(screen.getByText("No user matches that.")).toBeInTheDocument());
  });
});
