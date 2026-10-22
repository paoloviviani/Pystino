import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IdentityProvider, OidcPolicy } from "../lib/types";
import { AdminSettings, RedirectUri } from "./AdminSettings";

/**
 * The callback URL shown while an identity provider is being added.
 *
 * Worth pinning because the value is not guessable and the failure it prevents
 * is expensive: the path carries the connection's own name, the IdP rejects the
 * login unless it holds that exact string, and a wrong registration produces a
 * sign-in that works right up to the redirect back.
 */
describe("RedirectUri", () => {
  it("carries the name being typed, because the path does", () => {
    render(<RedirectUri name="gitlab" />);
    expect(
      screen.getByText(`${window.location.origin}/auth/callback/gitlab`),
    ).toBeInTheDocument();
  });

  it("shows the shape before a name is typed, rather than a broken URL", () => {
    // `.../auth/callback/` with nothing after it reads as the real answer and
    // is not one; the placeholder says a name is still owed.
    render(<RedirectUri name="  " />);
    expect(
      screen.getByText(`${window.location.origin}/auth/callback/<name>`),
    ).toBeInTheDocument();
    // Nothing to copy yet, so no button offering to.
    expect(
      screen.queryByRole("button", { name: "Copy" }),
    ).not.toBeInTheDocument();
  });

  it("tells the operator to register it exactly, not as a wildcard", () => {
    // An open redirect on an OIDC client hands the authorization code to
    // whoever asks (ADR 0035), so this is the one sentence that stays.
    render(<RedirectUri name="entra" />);
    expect(screen.getByText(/never a wildcard/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });
});

/**
 * The providers card, and specifically which rows offer User sync.
 *
 * What used to be easy to get wrong: the bundled Authelia's row is seeded
 * with an adapter it never runs (`authelia_file`, never confirmed), so the
 * badge it produced was a warning every bundled deployment carried and could
 * not act on — and its people are managed on the Users page, so there is
 * nothing to import at all. A disabled row (a provider moved away from)
 * syncs nothing either. Only an external, enabled row offers the screen.
 */
describe("AdminSettings providers card", () => {
  function provider(
    overrides: Partial<IdentityProvider> = {},
  ): IdentityProvider {
    return {
      id: "p1",
      name: "corp-idp",
      issuer: "https://idp.test/main",
      client_id: "pystino-console",
      has_client_secret: true,
      scopes: ["openid"],
      groups_claim: "groups",
      fetch_userinfo: false,
      group_mappings: [],
      link_by_email: false,
      group_sync: "never",
      is_enabled: true,
      source: "console",
      user_count: 0,
      removable: false,
      kept_reason: "",
      internal_base_url: "",
      logout_url: "",
      default_logout_url: "",
      kind: "generic",
      group_source: "none",
      admin_source: "console",
      admin_claim: "",
      admin_values: [],
      subject_claim: "sub",
      sync_adapter: "none",
      sync_interval_minutes: 60,
      sync_deprovision: "disable",
      sync_create_users: true,
      sync_confirmed: true,
      capabilities: {
        claims_groups: true,
        pull_adapters: ["authelia_file"],
        scim_push: false,
        subject_before_login: true,
        deprovision: true,
        creates_logins: false,
        adapters: ["authelia_file"],
      },
      ...overrides,
    };
  }

  const policy: OidcPolicy = {
    auto_provision: true,
    unknown_user_policy: "refuse",
    join_mode: "open",
    required_groups: [],
    group_fallback: "refuse",
    groups_claim: "groups",
    group_mappings: [],
    code_agents_mode: "on",
    api_keys_mode: "on",
    source: "environment",
    sources: {},
    configured: null,
    propagation_seconds: 10,
  };

  function renderScreen(element: ReactElement) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{element}</MemoryRouter>
      </QueryClientProvider>,
    );
  }

  function fetchFor(providers: IdentityProvider[]) {
    return vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test").pathname;
      if (url === "/api/admin/email") {
        return new Response(
          JSON.stringify({
            host: "",
            port: 0,
            username: "",
            from_address: "",
            has_password: false,
            source: "environment",
            enabled: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url === "/api/admin/oidc/policy") {
        return new Response(JSON.stringify(policy), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.endsWith("/sync/runs"))
        return new Response("[]", { status: 200 });
      if (url.endsWith("/directory"))
        return new Response("[]", { status: 200 });
      if (url === "/api/admin/identity-providers") {
        return new Response(JSON.stringify(providers), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
  }

  afterEach(() => vi.unstubAllGlobals());

  it("offers User sync… on an external enabled provider", async () => {
    vi.stubGlobal("fetch", fetchFor([provider()]));
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "User sync…" }),
      ).toBeInTheDocument(),
    );
  });

  it("hides the button and the sync badge on the bundled Authelia, whatever its seeded adapter", async () => {
    vi.stubGlobal(
      "fetch",
      fetchFor([
        provider({
          name: "authelia",
          kind: "authelia",
          sync_adapter: "authelia_file",
          sync_confirmed: false,
        }),
      ]),
    );
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(screen.getAllByText("authelia").length).toBeGreaterThan(0),
    );
    expect(
      screen.queryByRole("button", { name: "User sync…" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/sync: authelia_file/)).not.toBeInTheDocument();
    expect(screen.queryByText(/awaiting review/)).not.toBeInTheDocument();
  });

  it("hides the button and the sync badge on a disabled row", async () => {
    vi.stubGlobal(
      "fetch",
      fetchFor([
        provider({
          name: "old-idp",
          is_enabled: false,
          sync_adapter: "authelia_file",
          sync_confirmed: true,
        }),
      ]),
    );
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(screen.getByText("old-idp")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "User sync…" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/sync: authelia_file/)).not.toBeInTheDocument();
  });

  it("the dialog opens under its new name and says the screen is optional", async () => {
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", fetchFor([provider()]));
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "User sync…" }),
      ).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "User sync…" }));

    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: "User sync — corp-idp" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/Optional\. Keeps this console's user list in step/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Off \(default\) — people appear at their first sign-in/,
      ),
    ).toBeInTheDocument();
  });

  /**
   * Removing a spent previous provider. The API reports `removable`; the
   * console offers the button only on a disabled row that says so, and shows
   * the reason on one that does not.
   */
  describe("removing a previous provider", () => {
    it("offers Remove on a disabled row the API says is removable", async () => {
      vi.stubGlobal(
        "fetch",
        fetchFor([
          provider({ name: "previous-1", is_enabled: false, removable: true }),
        ]),
      );
      renderScreen(<AdminSettings />);

      await waitFor(() =>
        expect(screen.getByText("previous-1")).toBeInTheDocument(),
      );
      expect(
        screen.getByRole("button", { name: "Remove" }),
      ).toBeInTheDocument();
    });

    it("shows why a used row is kept, and no button", async () => {
      vi.stubGlobal(
        "fetch",
        fetchFor([
          provider({
            name: "previous-2",
            is_enabled: false,
            removable: false,
            user_count: 3,
            kept_reason:
              "Kept: 3 people signed in with it, so they can be linked back.",
          }),
        ]),
      );
      renderScreen(<AdminSettings />);

      await waitFor(() =>
        expect(screen.getByText("previous-2")).toBeInTheDocument(),
      );
      expect(
        screen.getByText(/Kept: 3 people signed in with it/),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Remove" }),
      ).not.toBeInTheDocument();
    });

    it("never offers Remove on the enabled row, even if a response said removable", async () => {
      vi.stubGlobal("fetch", fetchFor([provider({ removable: true })]));
      renderScreen(<AdminSettings />);

      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "User sync…" }),
        ).toBeInTheDocument(),
      );
      expect(
        screen.queryByRole("button", { name: "Remove" }),
      ).not.toBeInTheDocument();
    });

    it("asks first, deletes only on confirm, and cancel deletes nothing", async () => {
      const user = userEvent.setup({ delay: null });
      const fetcher = fetchFor([
        provider({
          id: "gone-1",
          name: "previous-3",
          is_enabled: false,
          removable: true,
        }),
      ]);
      vi.stubGlobal("fetch", fetcher);
      renderScreen(<AdminSettings />);

      const deletes = () =>
        fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE");

      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Remove" }),
        ).toBeInTheDocument(),
      );
      await user.click(screen.getByRole("button", { name: "Remove" }));
      const dialog = await screen.findByRole("dialog", {
        name: "Remove previous-3?",
      });
      expect(deletes()).toHaveLength(0);

      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() =>
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
      );
      expect(deletes()).toHaveLength(0);

      await user.click(screen.getByRole("button", { name: "Remove" }));
      const again = await screen.findByRole("dialog", {
        name: "Remove previous-3?",
      });
      await user.click(within(again).getByRole("button", { name: "Remove" }));

      await waitFor(() => expect(deletes()).toHaveLength(1));
      expect(String(deletes()[0]?.[0])).toBe(
        "/api/admin/identity-providers/gone-1",
      );
    });
  });
});

/**
 * The capabilities card (ADR 0096, amended): what new groups start with, and
 * the two bulk flips. What is pinned here is the shape of the decisions — the
 * default saves as its own policy field, the bulk flip previews before it
 * acts and says how many people it touches, and a preview answering zero
 * applies without stopping to confirm.
 */
describe("AdminSettings capabilities card", () => {
  function policyFixture(overrides: Partial<OidcPolicy> = {}): OidcPolicy {
    return {
      auto_provision: true,
      unknown_user_policy: "refuse",
      join_mode: "open",
      required_groups: [],
      group_fallback: "refuse",
      groups_claim: "groups",
      group_mappings: [],
      code_agents_mode: "on",
      api_keys_mode: "on",
      source: "environment",
      sources: {},
      configured: null,
      propagation_seconds: 10,
      ...overrides,
    };
  }

  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  function previewBody(counts: {
    would_gain?: number | null;
    would_lose?: number | null;
  }) {
    return new Response(
      JSON.stringify({
        active_users: 5,
        would_be_refused: 0,
        admins_exempt: 1,
        would_gain: null,
        would_lose: null,
        ...counts,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }

  function mockRoutes({
    policy,
    gains = 0,
    losses = 0,
    putStatus = 200,
    postStatus = 200,
  }: {
    policy: OidcPolicy;
    gains?: number;
    losses?: number;
    putStatus?: number;
    postStatus?: number;
  }) {
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://console.test");
        const method = init?.method ?? "GET";
        if (url.pathname === "/api/admin/oidc/policy/preview") {
          if (url.searchParams.get("bulk_value") === "true")
            return previewBody({ would_gain: gains });
          return previewBody({ would_lose: losses });
        }
        if (
          url.pathname === "/api/admin/groups/capabilities" &&
          method === "POST"
        ) {
          if (postStatus !== 200)
            return new Response(
              JSON.stringify({ error: { message: "the server refused that" } }),
              {
                status: postStatus,
                headers: { "content-type": "application/json" },
              },
            );
          return new Response(JSON.stringify({ updated: 4 }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.pathname === "/api/admin/oidc/policy" && method === "PUT") {
          if (putStatus !== 200)
            return new Response(
              JSON.stringify({ error: { message: "the server refused that" } }),
              {
                status: putStatus,
                headers: { "content-type": "application/json" },
              },
            );
          return new Response(JSON.stringify(policy), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.pathname === "/api/admin/email") {
          return new Response(
            JSON.stringify({
              host: "",
              port: 0,
              username: "",
              from_address: "",
              has_password: false,
              source: "environment",
              enabled: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.pathname === "/api/admin/oidc/policy") {
          return new Response(JSON.stringify(policy), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.pathname === "/api/admin/identity-providers") {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.pathname === "/api/admin/groups") {
          return new Response(JSON.stringify({ items: [], total: 0 }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    );
  }

  function renderScreen(element: ReactElement) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{element}</MemoryRouter>
      </QueryClientProvider>,
    );
  }

  const puts = () =>
    fetchMock.mock.calls
      .filter(
        ([url, init]) =>
          String(url).endsWith("/oidc/policy") &&
          (init as RequestInit).method === "PUT",
      )
      .map(([, init]) => JSON.parse((init as RequestInit).body as string));

  const posts = () =>
    fetchMock.mock.calls
      .filter(
        ([url, init]) =>
          String(url).includes("/groups/capabilities") &&
          (init as RequestInit).method === "POST",
      )
      .map(([, init]) => JSON.parse((init as RequestInit).body as string));

  async function openCard() {
    // Await the settled card, not its header: the policy query answers after
    // the first paint, and a test that stops at the header races the spinner.
    // Both capability rows carry a badge, so the wait matches two elements.
    return (await screen.findAllByText(/new groups: on|new groups: off/))[0]!;
  }

  it("seeds the two defaults from the policy in force", async () => {
    mockRoutes({ policy: policyFixture({ code_agents_mode: "off" }) });
    renderScreen(<AdminSettings />);
    await openCard();
    expect(screen.getByText("new groups: off")).toBeInTheDocument();
    expect(screen.getByText("new groups: on")).toBeInTheDocument();
  });

  it("saves a default as its own policy field", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({ policy: policyFixture() });
    renderScreen(<AdminSettings />);
    await openCard();
    // Two rows carry the same buttons; the buttons render in card order
    // (coding agents first), so index selects the row.
    await user.click(
      screen.getAllByRole("button", { name: /turn off for new groups/i })[0]!,
    );
    await waitFor(() => expect(puts()).toEqual([{ code_agents_mode: "off" }]));
  });

  it("previews a bulk turn-off and applies it on confirm", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({ policy: policyFixture(), losses: 2 });
    renderScreen(<AdminSettings />);
    await openCard();

    await user.click(
      screen.getAllByRole("button", { name: /turn off for all groups/i })[0]!,
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/2 people would lose/i),
    ).toBeInTheDocument();
    expect(posts()).toEqual([]);

    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() =>
      expect(posts()).toEqual([{ capability: "code_agents", value: false }]),
    );
  });

  it("applies straight away when the preview says nobody is affected", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({ policy: policyFixture(), gains: 0 });
    renderScreen(<AdminSettings />);
    await openCard();

    await user.click(
      screen.getAllByRole("button", { name: /turn on for all groups/i })[0]!,
    );
    await waitFor(() =>
      expect(posts()).toEqual([{ capability: "code_agents", value: true }]),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
