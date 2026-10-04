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
    expect(screen.getByText(`${window.location.origin}/auth/callback/gitlab`)).toBeInTheDocument();
  });

  it("shows the shape before a name is typed, rather than a broken URL", () => {
    // `.../auth/callback/` with nothing after it reads as the real answer and
    // is not one; the placeholder says a name is still owed.
    render(<RedirectUri name="  " />);
    expect(
      screen.getByText(`${window.location.origin}/auth/callback/<name>`),
    ).toBeInTheDocument();
    // Nothing to copy yet, so no button offering to.
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
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
  function provider(overrides: Partial<IdentityProvider> = {}): IdentityProvider {
    return {
      id: "p1",
      name: "keycloak",
      issuer: "https://idp.test/realms/main",
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
      kind: "keycloak",
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
        pull_adapters: ["keycloak_admin"],
        scim_push: false,
        subject_before_login: true,
        deprovision: true,
        adapters: ["keycloak_admin"],
      },
      ...overrides,
    };
  }

  const policy: OidcPolicy = {
    auto_provision: true,
    unknown_user_policy: "refuse",
    groups_claim: "groups",
    group_mappings: [],
    source: "environment",
    sources: {},
    configured: null,
    propagation_seconds: 10,
  };

  function renderScreen(element: ReactElement) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
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
      if (url.endsWith("/sync/runs")) return new Response("[]", { status: 200 });
      if (url.endsWith("/directory")) return new Response("[]", { status: 200 });
      if (url === "/api/admin/identity-providers") {
        return new Response(JSON.stringify(providers), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });
  }

  afterEach(() => vi.unstubAllGlobals());

  it("offers User sync… on an external enabled provider", async () => {
    vi.stubGlobal("fetch", fetchFor([provider()]));
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "User sync…" })).toBeInTheDocument(),
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

    await waitFor(() => expect(screen.getAllByText("authelia").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "User sync…" })).not.toBeInTheDocument();
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
          sync_adapter: "keycloak_admin",
          sync_confirmed: true,
        }),
      ]),
    );
    renderScreen(<AdminSettings />);

    await waitFor(() => expect(screen.getByText("old-idp")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "User sync…" })).not.toBeInTheDocument();
    expect(screen.queryByText(/sync: keycloak_admin/)).not.toBeInTheDocument();
  });

  it("the dialog opens under its new name and says the screen is optional", async () => {
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", fetchFor([provider()]));
    renderScreen(<AdminSettings />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "User sync…" })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "User sync…" }));

    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: "User sync — keycloak" })).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/Optional\. Keeps this console's user list in step/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Off \(default\) — people appear at their first sign-in/)).toBeInTheDocument();
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
        fetchFor([provider({ name: "previous-1", is_enabled: false, removable: true })]),
      );
      renderScreen(<AdminSettings />);

      await waitFor(() => expect(screen.getByText("previous-1")).toBeInTheDocument());
      expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
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
            kept_reason: "Kept: 3 people signed in with it, so they can be linked back.",
          }),
        ]),
      );
      renderScreen(<AdminSettings />);

      await waitFor(() => expect(screen.getByText("previous-2")).toBeInTheDocument());
      expect(screen.getByText(/Kept: 3 people signed in with it/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    });

    it("never offers Remove on the enabled row, even if a response said removable", async () => {
      vi.stubGlobal("fetch", fetchFor([provider({ removable: true })]));
      renderScreen(<AdminSettings />);

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "User sync…" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    });

    it("asks first, deletes only on confirm, and cancel deletes nothing", async () => {
      const user = userEvent.setup({ delay: null });
      const fetcher = fetchFor([
        provider({ id: "gone-1", name: "previous-3", is_enabled: false, removable: true }),
      ]);
      vi.stubGlobal("fetch", fetcher);
      renderScreen(<AdminSettings />);

      const deletes = () =>
        fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE");

      await waitFor(() => expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: "Remove" }));
      const dialog = await screen.findByRole("dialog", { name: "Remove previous-3?" });
      expect(deletes()).toHaveLength(0);

      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(deletes()).toHaveLength(0);

      await user.click(screen.getByRole("button", { name: "Remove" }));
      const again = await screen.findByRole("dialog", { name: "Remove previous-3?" });
      await user.click(within(again).getByRole("button", { name: "Remove" }));

      await waitFor(() => expect(deletes()).toHaveLength(1));
      expect(String(deletes()[0]?.[0])).toBe("/api/admin/identity-providers/gone-1");
    });
  });
});
