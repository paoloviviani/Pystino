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
    // Null rather than absent: `AdminUser.username` is `string | null`, and a
    // factory that left it out made every override widen it to include
    // `undefined` — which typechecks in isolation and fails the build.
    username: null,
    issuer: "https://idp.test",
    subject: `subject-${index}`,
    is_active: true,
    is_admin: false,
    has_password: false,
    linked_identities: [],
    membership_source: null,
    groups: ["research"],
    default_billing_group: "research",
    active_key_count: 1,
    last_login_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

interface Seen {
  users: URLSearchParams[];
  patches: { id: string; body: Record<string, unknown> }[];
}

/**
 * A fake directory of `total` accounts that honours limit, offset and q, so a
 * test can assert on what came back rather than only on what was asked for.
 *
 * The PATCH route merges into the stored row and answers with it, the way the
 * gateway does, so an edit-then-refetch cycle shows the change rather than
 * silently keeping the stale one.
 */
function routes(
  total: number,
  seen: Seen = { users: [], patches: [] },
  firstOverrides: Partial<AdminUser> = {},
) {
  const everyone = Array.from({ length: total }, (_, index) =>
    user(index, index === 0 ? firstOverrides : {}),
  );
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
    const patched = url.pathname.match(/^\/api\/admin\/users\/([^/]+)$/);
    if (init?.method === "PATCH" && patched) {
      const body = JSON.parse(String(init.body ?? "{}")) as Partial<AdminUser>;
      const row = everyone.find((entry) => entry.id === patched[1]);
      if (!row) return new Response(null, { status: 404 });
      Object.assign(row, body);
      seen.patches.push({ id: patched[1]!, body: body as Record<string, unknown> });
      return new Response(JSON.stringify(row), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
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
    const seen: Seen = { users: [], patches: [] };
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
    const seen: Seen = { users: [], patches: [] };
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
    const seen: Seen = { users: [], patches: [] };
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
    const seen: Seen = { users: [], patches: [] };
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
    const seen: Seen = { users: [], patches: [] };
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

/**
 * The edit dialog is where an administrator's decisions are made, so what is
 * pinned here is what used to be impossible or dishonest: the profile fields
 * are editable at all; a save sends only what changed (the gateway records a
 * sent profile field as administrator-edited, which stops sign-in refreshing
 * it from the directory — sending an untouched field would detach it under
 * the guise of "no change"); clearing writes null rather than pretending;
 * and the identity pair is shown but named as not editable, because a value
 * that looks like a form field invites exactly the edit that cannot work.
 */
describe("AdminUsers edit dialog", () => {
  it("saves a changed profile field and shows the result", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [] };
    vi.stubGlobal("fetch", routes(5, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Edit" })[0]!!);
    const dialog = screen.getByRole("dialog");

    await user_.clear(within(dialog).getByLabelText("Display name"));
    await user_.type(within(dialog).getByLabelText("Display name"), "Renamed Person");
    await user_.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(seen.patches).toHaveLength(1));
    const body = seen.patches[0]!.body;
    expect(body.display_name).toBe("Renamed Person");
    // Untouched fields do not travel: sending one would record it as
    // administrator-edited and detach it from the directory.
    expect(body).not.toHaveProperty("email");
    expect(body).not.toHaveProperty("username");
    // The flags keep their shape: the dialog has always sent both.
    expect(body.is_active).toBe(true);
    expect(body.is_admin).toBe(false);

    // The row reflects the save, not the cache's stale copy.
    await waitFor(() => expect(screen.getByText("Renamed Person")).toBeInTheDocument());
  });

  it("clears a field with null rather than an empty string", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [] };
    vi.stubGlobal("fetch", routes(5, seen, { username: "person-0@local" }));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@local")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Edit" })[0]!!);
    const dialog = screen.getByRole("dialog");

    await user_.clear(within(dialog).getByLabelText("Username"));
    await user_.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(seen.patches).toHaveLength(1));
    expect(seen.patches[0]!.body.username).toBeNull();
  });

  it("re-seeds the fields when another user opens", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(5));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Edit" })[0]!!);
    const dialog = screen.getByRole("dialog");
    await waitFor(() =>
      expect(screen.getByLabelText("Display name")).toHaveValue("Person 0"),
    );
    // Both closers — the corner X and the footer button — share the name.
    await user_.click(within(dialog).getAllByRole("button", { name: "Close" })[0]!);

    await user_.click(screen.getAllByRole("button", { name: "Edit" })[1]!);
    await waitFor(() =>
      expect(screen.getByLabelText("Display name")).toHaveValue("Person 1"),
    );
  });

  it("shows the identity pair read-only", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(5));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Edit" })[0]!!);

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("https://idp.test / subject-0")).toBeInTheDocument();
    expect(within(dialog).getByText(/the login identity/i)).toBeInTheDocument();
  });

  it("annotates a directory account's profile fields with the sign-in refresh", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(5));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Edit" })[0]!!);

    const dialog = screen.getByRole("dialog");
    // Says what actually happens, not a half of it: editing pins the value
    // against the directory's refresh.
    expect(
      within(dialog).getByText(/Editing it records your value, and sign-in stops changing it/),
    ).toBeInTheDocument();
  });
});
