import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import type { AdminUser, DeletePreview, IdentityProvider, MergePreview } from "../lib/types";
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
    authelia_sync: null,
    authelia_sync_message: null,
    ...overrides,
  };
}

/** A bundled Authelia row, the shape `useBundledProvider` looks for: the one
 * enabled row, of this kind. Only the fields the page actually reads are
 * filled with anything meaningful; the rest are placeholders. */
function bundledProvider(overrides: Partial<IdentityProvider> = {}): IdentityProvider {
  return {
    id: "provider-authelia",
    name: "authelia",
    issuer: "https://gw.test/authelia",
    client_id: "pystino-console",
    has_client_secret: true,
    scopes: ["openid"],
    groups_claim: "groups",
    fetch_userinfo: false,
    group_mappings: [],
    link_by_email: false,
    group_sync: "never",
    is_enabled: true,
    source: "environment",
    user_count: 0,
    internal_base_url: "",
    logout_url: "",
    default_logout_url: "",
    kind: "authelia",
    group_source: "none",
    admin_source: "console",
    admin_claim: "",
    admin_values: [],
    subject_claim: "sub",
    sync_adapter: "authelia_file",
    sync_interval_minutes: 60,
    sync_deprovision: "disable",
    sync_create_users: true,
    sync_confirmed: true,
    capabilities: {
      claims_groups: false,
      pull_adapters: [],
      scim_push: false,
      subject_before_login: true,
      deprovision: true,
      adapters: ["authelia_file"],
    },
    ...overrides,
  };
}

interface Seen {
  users: URLSearchParams[];
  patches: { id: string; body: Record<string, unknown> }[];
  posts: { path: string; body: Record<string, unknown> }[];
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
  seen: Seen = { users: [], patches: [], posts: [] },
  firstOverrides: Partial<AdminUser> = {},
  providers: IdentityProvider[] = [],
) {
  const everyone = Array.from({ length: total }, (_, index) =>
    user(index, index === 0 ? firstOverrides : {}),
  );
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = init?.method ?? "GET";
    const body = () => JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;

    if (url.pathname === "/api/admin/identity-providers") {
      // Not paginated (`GET /admin/identity-providers` answers a bare list),
      // unlike every other listing route this file mocks — `jsonResponse`
      // would wrap it in a page envelope no caller here expects.
      return new Response(JSON.stringify(providers), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.pathname === "/api/admin/identity-events") {
      return jsonResponse([]);
    }
    if (url.pathname === "/api/admin/users" && method === "GET") {
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
    if (url.pathname === "/api/admin/users" && method === "POST") {
      const payload = body();
      seen.posts.push({ path: url.pathname, body: payload });
      const created = user(everyone.length, {
        id: `created-${everyone.length}`,
        email: String(payload.email ?? ""),
        display_name: String(payload.display_name ?? ""),
        groups: (payload.groups as string[] | undefined) ?? [],
      });
      everyone.push(created);
      return new Response(JSON.stringify({ ...created, password: "one-time-pw-123" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }
    const signIn = url.pathname.match(/^\/api\/admin\/users\/([^/]+)\/sign-in$/);
    if (method === "POST" && signIn) {
      const payload = body();
      seen.posts.push({ path: url.pathname, body: payload });
      const row = everyone.find((entry) => entry.id === signIn[1]);
      if (!row) return new Response(null, { status: 404 });
      return new Response(JSON.stringify({ ...row, password: "one-time-pw-456" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }
    const resetPassword = url.pathname.match(/^\/api\/admin\/users\/([^/]+)\/reset-password$/);
    if (method === "POST" && resetPassword) {
      seen.posts.push({ path: url.pathname, body: {} });
      return jsonResponse({ password: "one-time-pw-789" });
    }
    const patched = url.pathname.match(/^\/api\/admin\/users\/([^/]+)$/);
    if (method === "PATCH" && patched) {
      const payload = body();
      const row = everyone.find((entry) => entry.id === patched[1]);
      if (!row) return new Response(null, { status: 404 });
      Object.assign(row, payload);
      seen.patches.push({ id: patched[1]!, body: payload });
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    const seen: Seen = { users: [], patches: [], posts: [] };
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
    // The admin flag keeps its shape: the dialog has always sent it.
    // `is_active` moved to its own Disable/Enable dialog (ADR 0093 §9.1) and
    // is never part of this save.
    expect(body.is_admin).toBe(false);
    expect(body).not.toHaveProperty("is_active");

    // The row reflects the save, not the cache's stale copy.
    await waitFor(() => expect(screen.getByText("Renamed Person")).toBeInTheDocument());
  });

  it("clears a field with null rather than an empty string", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [], posts: [] };
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

/**
 * The bundled Authelia's own actions (ADR 0093 §8.1), pinned on the two
 * things that used to be easy to get wrong: which action a row offers
 * depends on whether the person already has a bundled login, and every
 * action disappears — with an explanation — once the deployment's one
 * enabled provider is an external IdP instead.
 */
describe("AdminUsers bundled Authelia actions", () => {
  it("offers Add user and Create sign-in while the bundled Authelia is active", async () => {
    vi.stubGlobal("fetch", routes(3, undefined, {}, [bundledProvider()]));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Add user" })).toBeInTheDocument(),
    );
    expect(screen.getAllByRole("button", { name: "Create sign-in" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Reset password" })).not.toBeInTheDocument();
  });

  it("offers Reset password instead, for a person who already has one", async () => {
    vi.stubGlobal(
      "fetch",
      routes(1, undefined, { issuer: bundledProvider().issuer }, [bundledProvider()]),
    );
    renderScreen(<AdminUsers />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reset password" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Create sign-in" })).not.toBeInTheDocument();
  });

  it("hides every bundled action and explains accounts live at the identity provider, with an external IdP", async () => {
    vi.stubGlobal(
      "fetch",
      routes(1, undefined, {}, [bundledProvider({ kind: "keycloak", is_enabled: true })]),
    );
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    expect(
      screen.getByText(/Accounts live at the identity provider/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add user" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create sign-in" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset password" })).not.toBeInTheDocument();
    // Disable/Enable is a gateway-side action regardless of provider kind.
    expect(screen.getAllByRole("button", { name: "Disable" }).length).toBeGreaterThan(0);
  });

  it("Add user mints a one-time password with a copy button and the sharing note", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [], posts: [] };
    vi.stubGlobal("fetch", routes(1, seen, {}, [bundledProvider()]));
    renderScreen(<AdminUsers />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Add user" })).toBeInTheDocument(),
    );
    await user_.click(screen.getByRole("button", { name: "Add user" }));
    const dialog = screen.getByRole("dialog");

    await user_.type(within(dialog).getByLabelText("Login"), "frank");
    await user_.type(within(dialog).getByLabelText("Email"), "frank@example.org");
    await user_.click(within(dialog).getByRole("button", { name: "Add user" }));

    await waitFor(() => expect(seen.posts).toHaveLength(1));
    expect(seen.posts[0]!.body).toMatchObject({ login: "frank", email: "frank@example.org" });
    expect(within(dialog).getByText("one-time-pw-123")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Share it over a one-time channel/),
    ).toBeInTheDocument();
  });

  it("Reset password mints a fresh password for a person with a bundled login", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [], posts: [] };
    vi.stubGlobal(
      "fetch",
      routes(1, seen, { issuer: bundledProvider().issuer }, [bundledProvider()]),
    );
    renderScreen(<AdminUsers />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reset password" })).toBeInTheDocument(),
    );
    await user_.click(screen.getByRole("button", { name: "Reset password" }));
    const dialog = screen.getByRole("dialog");
    await user_.click(within(dialog).getByRole("button", { name: "Reset password" }));

    await waitFor(() => expect(within(dialog).getByText("one-time-pw-789")).toBeInTheDocument());
    expect(seen.posts).toHaveLength(1);
  });
});

/**
 * Disable and Enable, as their own dialog: what used to be a plain checkbox
 * now has to say what actually happens before it happens (ADR 0093 §9.1).
 */
describe("AdminUsers disable and enable", () => {
  it("states the consequences and disables on confirm", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [], posts: [] };
    vi.stubGlobal("fetch", routes(1, seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getByRole("button", { name: "Disable" }));
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByText(/Personal API keys are kept/)).toBeInTheDocument();
    await user_.click(within(dialog).getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(seen.patches).toHaveLength(1));
    expect(seen.patches[0]!.body.is_active).toBe(false);
  });

  it("offers Enable for a disabled account, and says keys and sessions stay revoked", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen: Seen = { users: [], patches: [], posts: [] };
    vi.stubGlobal("fetch", routes(1, seen, { is_active: false }));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getByRole("button", { name: "Enable" }));
    const dialog = screen.getByRole("dialog");

    expect(
      within(dialog).getByText(/Sessions, minted keys and devices stay revoked/),
    ).toBeInTheDocument();
    await user_.click(within(dialog).getByRole("button", { name: "Enable" }));

    await waitFor(() => expect(seen.patches).toHaveLength(1));
    expect(seen.patches[0]!.body.is_active).toBe(true);
  });
});

function deletePreview(overrides: Partial<DeletePreview> = {}): DeletePreview {
  return {
    user_id: "u0",
    gateway_counts: { api_keys: 1 },
    bundled_login: null,
    chat_counts: { conversations: 2 },
    chat_reachable: true,
    shared: [],
    shared_with_others: false,
    chat_unattributed_legacy_shares: 0,
    ...overrides,
  };
}

/**
 * The delete dialog's own gate (ADR 0093 §9.2): a shared resource must be
 * acknowledged, not merely counted, before the account can be erased. What
 * used to be easy to get wrong is the two states this covers — the tick box
 * only exists, and only blocks the button, when the preview actually reports
 * something shared.
 */
describe("AdminUsers delete dialog", () => {
  function deleteRoutes(preview: DeletePreview, seen: { deletes: Record<string, unknown>[] }) {
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const method = init?.method ?? "GET";
      if (url.pathname === "/api/admin/identity-providers") {
        return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.pathname === "/api/admin/identity-events") return jsonResponse([]);
      if (url.pathname === "/api/admin/erasures/pending") return jsonResponse({ pending: 0 });
      if (url.pathname === "/api/admin/users" && method === "GET") return jsonResponse([user(0)]);
      if (url.pathname === "/api/admin/users/u0/delete-preview") return jsonResponse(preview);
      if (url.pathname === "/api/admin/users/u0" && method === "DELETE") {
        seen.deletes.push(JSON.parse(String(init?.body ?? "{}")));
        return jsonResponse({ erasure_id: "e1", chat_erasure_done: true });
      }
      return jsonResponse([]);
    });
  }

  it("disables Delete permanently until the shared-loss box is ticked, when shares exist", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen = { deletes: [] as Record<string, unknown>[] };
    vi.stubGlobal(
      "fetch",
      deleteRoutes(
        deletePreview({
          shared: [
            {
              kind: "shared_conversation",
              id: "s1",
              title: "A shared chat",
              audience: "anyone with the link",
            },
          ],
          shared_with_others: true,
        }),
        seen,
      ),
    );
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");

    await waitFor(() => expect(within(dialog).getByText("A shared chat — anyone with the link")).toBeInTheDocument());
    const confirmButton = within(dialog).getByRole("button", { name: "Delete permanently" });
    expect(confirmButton).toBeDisabled();

    await user_.click(within(dialog).getByRole("checkbox"));
    expect(confirmButton).toBeEnabled();

    await user_.click(confirmButton);
    await waitFor(() => expect(seen.deletes).toHaveLength(1));
    expect(seen.deletes[0]).toEqual({ confirm_shared_loss: true });
  });

  it("leaves Delete permanently enabled, with no tick box, when nothing is shared", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen = { deletes: [] as Record<string, unknown>[] };
    vi.stubGlobal("fetch", deleteRoutes(deletePreview(), seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");

    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Delete permanently" })).toBeEnabled(),
    );
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  });
});

function mergePreview(overrides: Partial<MergePreview> = {}): MergePreview {
  return {
    source_id: "u0",
    target_id: "u1",
    counts: { api_keys: 1 },
    identities_moving: [],
    identities_dropped: [],
    resulting_is_admin: false,
    bundled_logins_disabled: [],
    duplicate_rules_dropped: 0,
    chat_note: "the chat folds this person's conversations into the target at their next activity",
    ...overrides,
  };
}

/**
 * The merge dialog's own gate (ADR 0093 §7.1): irreversible, so the button
 * stays off until the operator has typed the source's own address back,
 * exactly, not merely selected a target and clicked through.
 */
describe("AdminUsers merge dialog", () => {
  function mergeRoutes(preview: MergePreview, seen: { merges: Record<string, unknown>[] }) {
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const method = init?.method ?? "GET";
      if (url.pathname === "/api/admin/identity-providers") {
        return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.pathname === "/api/admin/identity-events") return jsonResponse([]);
      if (url.pathname === "/api/admin/erasures/pending") return jsonResponse({ pending: 0 });
      if (url.pathname === "/api/admin/users" && method === "GET") {
        const q = url.searchParams.get("q") ?? "";
        const everyone = [user(0), user(1)];
        const matched = q
          ? everyone.filter((entry) => entry.email?.includes(q))
          : everyone;
        return jsonResponse(matched);
      }
      if (url.pathname === "/api/admin/users/u0/merge-preview") return jsonResponse(preview);
      if (url.pathname === "/api/admin/users/u0/merge" && method === "POST") {
        seen.merges.push(JSON.parse(String(init?.body ?? "{}")));
        return jsonResponse({
          target_id: "u1",
          counts: {},
          identities_dropped: [],
          bundled_logins_disabled: [],
          duplicate_rules_dropped: 0,
        });
      }
      return jsonResponse([]);
    });
  }

  it("disables Merge, irreversibly until the typed confirmation matches the source", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen = { merges: [] as Record<string, unknown>[] };
    vi.stubGlobal("fetch", mergeRoutes(mergePreview(), seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Merge into…" })[0]!);
    const dialog = screen.getByRole("dialog");

    await user_.type(within(dialog).getByPlaceholderText("Search by email, name or username"), "person-1");
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: /person-1@example.org/ })).toBeInTheDocument(),
    );
    await user_.click(within(dialog).getByRole("button", { name: /person-1@example.org/ }));

    await waitFor(() => expect(within(dialog).getByLabelText(/Type "/)).toBeInTheDocument());
    const confirmButton = within(dialog).getByRole("button", { name: "Merge, irreversibly" });
    expect(confirmButton).toBeDisabled();

    await user_.type(within(dialog).getByLabelText(/Type "/), "not the right address");
    await user_.type(within(dialog).getByLabelText("Reason"), "same person");
    expect(confirmButton).toBeDisabled();

    await user_.clear(within(dialog).getByLabelText(/Type "/));
    await user_.type(within(dialog).getByLabelText(/Type "/), "person-0@example.org");
    expect(confirmButton).toBeEnabled();

    await user_.click(confirmButton);
    await waitFor(() => expect(seen.merges).toHaveLength(1));
    expect(seen.merges[0]).toMatchObject({
      into: "u1",
      confirm: "person-0@example.org",
      reason: "same person",
    });
  });

  it("stays disabled with a matching confirmation but no reason", async () => {
    const user_ = userEvent.setup({ delay: null });
    const seen = { merges: [] as Record<string, unknown>[] };
    vi.stubGlobal("fetch", mergeRoutes(mergePreview(), seen));
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getAllByRole("button", { name: "Merge into…" })[0]!);
    const dialog = screen.getByRole("dialog");

    await user_.type(within(dialog).getByPlaceholderText("Search by email, name or username"), "person-1");
    await user_.click(
      await within(dialog).findByRole("button", { name: /person-1@example.org/ }),
    );

    await waitFor(() => expect(within(dialog).getByLabelText(/Type "/)).toBeInTheDocument());
    await user_.type(within(dialog).getByLabelText(/Type "/), "person-0@example.org");

    expect(within(dialog).getByRole("button", { name: "Merge, irreversibly" })).toBeDisabled();
    expect(seen.merges).toHaveLength(0);
  });
});

describe("AdminUsers activity", () => {
  it("lists identity events for the person", async () => {
    const user_ = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://console.test");
        if (url.pathname === "/api/admin/identity-events") {
          return jsonResponse([
            {
              id: "e1",
              at: "2026-08-01T10:00:00Z",
              actor_type: "user",
              actor_user_id: "admin-1",
              actor_label: "root@example.org",
              action: "user.disable",
              target_user_id: "u0",
              target_label: "person-0@example.org",
              issuer: null,
              subject: null,
              detail: {},
              reason: null,
            },
          ]);
        }
        // Not paginated — a bare list, unlike the routes `jsonResponse` wraps.
        if (url.pathname === "/api/admin/identity-providers") {
          return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
        }
        if (url.pathname === "/api/admin/users") return jsonResponse([user(0)]);
        return jsonResponse([]);
      }),
    );
    renderScreen(<AdminUsers />);

    await waitFor(() => expect(screen.getByText("person-0@example.org")).toBeInTheDocument());
    await user_.click(screen.getByRole("button", { name: "Activity" }));

    await waitFor(() => expect(screen.getByText("user.disable")).toBeInTheDocument());
    expect(screen.getByText("root@example.org")).toBeInTheDocument();
  });
});
