import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Me } from "../lib/types";
import { Shell } from "./Shell";

/**
 * The header, and specifically signing out.
 *
 * Worth its own tests because logout is the one control in the console whose
 * failure is a security problem rather than an inconvenience: a button that
 * appears to work and leaves the session cookie in place is worse than no
 * button.
 *
 * The menu's entries are Base UI menu items (ADR 0047), so their accessible
 * roles are the ARIA menu pattern's — `menuitem`, `menuitemcheckbox` — not
 * `button`/`checkbox`. Querying the pattern's roles is the point: if a refactor
 * ever drops them, the screen reader contract broke, and these tests say so.
 */

function me(overrides: Partial<Me> = {}): Me {
  return {
    id: "u1",
    email: "dave@example.org",
    display_name: "Dave",
    is_admin: true,
    groups: [{ id: "g1", name: "platform-admins", description: null }],
    default_billing_group: { id: "g1", name: "platform-admins", description: null },
    issuer: "local",
    has_password: true,
    capabilities: { code_agents: true, api_keys: true },
    ...overrides,
  };
}

function renderShell(user: Me = me(), path = "/") {
  // The account menu carries a mutation (change password), so the shell needs
  // the provider its real app wraps it in.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Shell me={user}>
          <p>content</p>
        </Shell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const assign = vi.fn();

/**
 * Menu interaction goes through `fireEvent.click`, not `userEvent`.
 *
 * Base UI's menu reads the pointer *sequence* — pointerdown/up timings and
 * coordinates — to tell an opening press from an outside press that should
 * dismiss. jsdom synthesises that sequence with zero deltas and no layout, so
 * `userEvent.click` races it and the menu intermittently never opens; a plain
 * click event is the behaviour the component is asked to deliver, and it is
 * deterministic. Found while migrating the menu to Base UI (ADR 0047).
 */
function clickMenuTrigger() {
  fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  assign.mockClear();
});

function stubNavigation() {
  // jsdom refuses a real navigation, and the assertion is about *whether* we
  // navigate, not where the browser ends up.
  vi.stubGlobal("location", { assign, href: "http://console.test/" });
}

describe("Shell", () => {
  it("shows who is signed in", () => {
    renderShell();
    expect(screen.getByRole("button", { name: /Dave/ })).toBeInTheDocument();
  });

  it("keeps the sign-out action behind a disclosure", () => {
    // Not hidden for its own sake: the header already carries the name and the
    // admin badge, and a fourth item in that row crowds it on a laptop.
    renderShell();
    expect(screen.queryByRole("menuitem", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("opens the menu and offers to sign out", async () => {
    renderShell();

    clickMenuTrigger();
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByText("dave@example.org")).toBeInTheDocument();
  });

  it("posts to the logout endpoint, and only on the button", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    stubNavigation();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(call[0])).toContain("/auth/logout");
    expect(call[1].method).toBe("POST");
  });

  it("leaves the page even when the logout request fails", async () => {
    // The cookie may already be gone, or the network may be down. Staying put
    // on an authenticated-looking page is the one outcome that is not
    // acceptable.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    stubNavigation();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/console/login"));
  });

  it("reloads the page rather than routing, so no stale data survives", async () => {
    // Every cached query in this tab was fetched as the previous user. A
    // client-side route change would leave that data in memory.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    stubNavigation();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/console/login"));
  });

  it("goes to the provider's end-session URL, not just to our login page", async () => {
    // The bug this exists to catch: dropping our own cookie leaves Keycloak's
    // SSO session standing, so /auth/login is answered without a password
    // prompt and the reader lands back on the console as the same person.
    // Signing out looked like it did nothing.
    const endSession =
      "http://idp.test/realms/llm-platform/protocol/openid-connect/logout?client_id=llm-gateway";
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ status: "ok", redirect_to: endSession }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    stubNavigation();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith(endSession));
  });

  it("falls back to our login page when the provider publishes no end-session URL", async () => {
    // Optional in the spec. Our session is gone either way, which is as much
    // as the gateway can promise on its own.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ status: "ok", redirect_to: null }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    stubNavigation();
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/console/login"));
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup({ delay: null });
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("menuitem", { name: "Sign out" })).not.toBeInTheDocument(),
    );
  });

  it("hides admin navigation from a non-administrator", () => {
    renderShell(me({ is_admin: false }));
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
  });

  it("does not offer the way into administration to a non-administrator", () => {
    renderShell(me({ is_admin: false }));
    expect(screen.queryByRole("link", { name: "Admin view" })).not.toBeInTheDocument();
  });

  it("keeps administration out of the navigation until you are in it", () => {
    // The point of the split: an administrator reading their own spend is not
    // administering anything, and ten section links beside that page said
    // otherwise.
    renderShell(me({ is_admin: true }));
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Your usage" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Admin view" })).toBeInTheDocument();
  });

  it("swaps the navigation for the admin sections once inside", () => {
    // Driven by the route, not by a toggle: somebody following a link straight
    // to /admin/models must arrive with the right navigation around it.
    renderShell(me({ is_admin: true }), "/admin/models");
    expect(screen.getByRole("link", { name: "Providers" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Quotas" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Your usage" })).not.toBeInTheDocument();
    // And the way back is where the way in was.
    expect(screen.getByRole("link", { name: "User view" })).toBeInTheDocument();
  });

  it("shows a non-administrator their own navigation even at an admin path", () => {
    // RequireAdmin refuses the page; the navigation must not imply otherwise.
    renderShell(me({ is_admin: false }), "/admin/models");
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
  });
});

/**
 * The sidebar, and specifically its shape in administration.
 *
 * The order of the links is worth pinning for the same reason the landing
 * page's order is: it is edited whenever a screen is added, and it has to
 * agree with routes/Admin.tsx — both read lib/adminNav.ts, so the grouping is
 * one list, but these tests are what notice if a screen stops being reached.
 */
describe("Shell: the sidebar", () => {
  afterEach(() => {
    try {
      window.localStorage.clear();
    } catch {
      // Nothing to clear if storage is unavailable, which is also fine.
    }
  });

  it("carries the title as the way home", () => {
    // What a wordmark does everywhere else on the web, and the shortest route
    // back to your own overview from six levels into administration.
    renderShell(me({ is_admin: true }), "/admin/models");
    const brand = screen.getByRole("link", { name: "Pystino LLM gateway" });
    expect(brand).toHaveAttribute("href", "/");
  });

  it("groups the admin navigation under the five categories", () => {
    renderShell(me({ is_admin: true }), "/admin/models");

    const sidebar = screen.getByRole("navigation", { name: "Administration" });
    for (const heading of ["Usage & limits", "Serving", "Privacy", "People", "Deployment"]) {
      expect(within(sidebar).getByText(heading)).toBeInTheDocument();
    }

    // The landing page leads, then the groups in their order — the same order
    // routes/Admin.tsx renders its cards in.
    const hrefs = within(sidebar)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual([
      "/admin",
      "/admin/reports",
      "/admin/quotas",
      "/admin/providers",
      "/admin/models",
      "/admin/routing",
      "/admin/search",
      "/admin/redaction",
      "/admin/users",
      "/admin/groups",
      "/admin/audit",
      "/admin/settings",
    ]);
    // The view switcher is deliberately not one of the links — it sits pinned
    // under the navigation, the sidebar's one control.
    expect(screen.getByRole("link", { name: "User view" })).toHaveAttribute("href", "/");
  });

  it("collapses to a rail: the labels go, the named links stay", () => {
    renderShell(me({ is_admin: true }), "/admin/models");

    fireEvent.click(screen.getByRole("button", { name: "Collapse the navigation" }));

    expect(screen.queryByText("Providers")).not.toBeInTheDocument();
    // The accessible name survives the text's removal — on the link itself,
    // and on the tooltip that stands in for it.
    expect(screen.getByRole("link", { name: "Providers" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand the navigation" })).toBeInTheDocument();
  });

  it("remembers the collapsed rail across a reload", () => {
    const first = renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Collapse the navigation" }));
    first.unmount();

    renderShell();
    expect(screen.queryByText("Your usage")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Your usage" })).toBeInTheDocument();
  });

  it("opens as a drawer on a phone, and closes from inside it", () => {
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByRole("button", { name: "Menu" })).toHaveAttribute("aria-expanded", "true");
    // The backdrop and the drawer's own close button share the name; both are
    // ways out, and both are named.
    const waysOut = screen.getAllByRole("button", { name: "Close the navigation" });
    expect(waysOut).toHaveLength(2);

    fireEvent.click(waysOut[0]!);
    // The backdrop unmounts; the drawer's button stays in the DOM (the phone
    // hides it with CSS, which jsdom does not evaluate) — what is observable
    // is that there is one way out left and the hamburger says it is shut.
    expect(screen.getAllByRole("button", { name: "Close the navigation" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Menu" })).toHaveAttribute("aria-expanded", "false");
  });
});

/**
 * The exact-figures preference.
 *
 * Amounts are rounded to milli-units everywhere, because the ledger's twelve
 * decimal places are noise on a screen someone is reading. This is the escape
 * hatch for the one reader who needs them — an administrator reconciling against
 * a provider's invoice, where a divergence can be smaller than a milli-unit and
 * still be the thing they are looking for.
 */
describe("Shell: exact figures", () => {
  afterEach(() => {
    try {
      window.localStorage.clear();
    } catch {
      // Nothing to clear if storage is unavailable, which is also fine.
    }
  });

  it("offers the toggle to an administrator", async () => {
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    const toggle = screen.getByRole("menuitemcheckbox", { name: /Exact figures/ });
    // Off by default: the safe default is the readable one, and exactness is a
    // deliberate act.
    expect(toggle).not.toBeChecked();
  });

  it("does not offer it to a reader who has no use for it", async () => {
    // Every figure that needs reconciling against an invoice is on a screen a
    // non-administrator cannot open.
    renderShell(me({ is_admin: false }));

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    expect(screen.queryByRole("menuitemcheckbox", { name: /Exact figures/ })).not.toBeInTheDocument();
  });

  it("remembers the choice across a reload", async () => {
    const first = renderShell();

    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /Exact figures/ }));
    await waitFor(() =>
      expect(screen.getByRole("menuitemcheckbox", { name: /Exact figures/ })).toBeChecked(),
    );

    first.unmount();
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    expect(screen.getByRole("menuitemcheckbox", { name: /Exact figures/ })).toBeChecked();
  });

  it("does not restore an administrator's choice for a non-administrator", async () => {
    // Same browser, different person: the preference is stored per browser, so
    // the admin check has to be applied on read as well as on render.
    const first = renderShell();
    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /Exact figures/ }));
    first.unmount();

    renderShell(me({ is_admin: false }));
    fireEvent.click(screen.getByRole("button", { name: /Dave/ }));
    expect(screen.queryByRole("menuitemcheckbox", { name: /Exact figures/ })).not.toBeInTheDocument();
  });
});

/**
 * Choosing a default billing group from the identity menu (ADR 0078).
 *
 * Until now this sentence was the whole feature: a person with no default
 * learned they had none and had no way, anywhere in the console, to fix it —
 * the gateway's own 403 named `PUT /api/me/default-billing-group` as the
 * remedy, and nothing in the UI called it. These tests are the picker that
 * closes that gap, and the refusal path, so a group the server rejects still
 * shows the reason rather than closing quietly.
 */
describe("Shell: default billing group", () => {
  it("offers to set one when there is none and the user has groups", () => {
    renderShell(me({ default_billing_group: null }));

    clickMenuTrigger();
    expect(screen.getByText("No default billing group")).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Set default billing group…" }),
    ).toBeInTheDocument();
  });

  it("offers to change one that is already set", () => {
    renderShell();

    clickMenuTrigger();
    expect(screen.getByText("Billing to platform-admins")).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Change default billing group…" }),
    ).toBeInTheDocument();
  });

  it("offers nothing to pick from when the user belongs to no group", () => {
    // A picker with zero options is worse than the plain sentence it would
    // replace.
    renderShell(me({ groups: [], default_billing_group: null }));

    clickMenuTrigger();
    expect(screen.getByText("No default billing group")).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /billing group…/ })).not.toBeInTheDocument();
  });

  it("saves the chosen group through the self-service endpoint", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(me()), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderShell(
      me({
        groups: [
          { id: "g1", name: "platform-admins", description: null },
          { id: "g2", name: "finance", description: null },
        ],
      }),
    );
    clickMenuTrigger();
    fireEvent.click(screen.getByRole("menuitem", { name: "Change default billing group…" }));
    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "g2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(call[0])).toContain("/api/me/default-billing-group");
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(call[1].body as string)).toEqual({ group_id: "g2" });
  });

  it("shows the server's refusal rather than closing quietly", async () => {
    // The membership check the endpoint already makes (ADR 0061) — the picker
    // adds no copy of it, so a stale option (left another group since opening
    // the menu) has to surface the server's own answer.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ error: { message: "You are not a member of that group." } }),
            { status: 403, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    renderShell();

    clickMenuTrigger();
    fireEvent.click(screen.getByRole("menuitem", { name: "Change default billing group…" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByText("You are not a member of that group.")).toBeInTheDocument(),
    );
  });

  it("resets to the current default each time it is reopened", () => {
    // The dialog stays mounted behind `open` so Base UI can animate the
    // close; a value seeded once into its own state would keep showing
    // whatever was last picked, not the account's actual default.
    renderShell(
      me({
        groups: [
          { id: "g1", name: "platform-admins", description: null },
          { id: "g2", name: "finance", description: null },
        ],
      }),
    );
    clickMenuTrigger();
    fireEvent.click(screen.getByRole("menuitem", { name: "Change default billing group…" }));
    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "g2" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    clickMenuTrigger();
    fireEvent.click(screen.getByRole("menuitem", { name: "Change default billing group…" }));
    expect(screen.getByLabelText("Group")).toHaveValue("g1");
  });
});
