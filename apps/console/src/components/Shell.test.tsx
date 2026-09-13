import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("makes the wordmark the way home", () => {
    // What a wordmark does everywhere else on the web, and the shortest route
    // back to your own overview from six levels into administration.
    renderShell(me({ is_admin: true }), "/admin/models");
    const brand = screen.getByRole("link", { name: /Pystino/ });
    expect(brand).toHaveAttribute("href", "/");
  });

  it("does not offer the way into administration to a non-administrator", () => {
    renderShell(me({ is_admin: false }));
    expect(screen.queryByRole("link", { name: "Admin view" })).not.toBeInTheDocument();
  });

  it("keeps administration out of the header until you are in it", () => {
    // The point of the split: an administrator reading their own spend is not
    // administering anything, and six section links above that page said
    // otherwise.
    renderShell(me({ is_admin: true }));
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Your usage" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Admin view" })).toBeInTheDocument();
  });

  it("swaps the header for the admin sections once inside", () => {
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
    // RequireAdmin refuses the page; the header must not imply otherwise.
    renderShell(me({ is_admin: false }), "/admin/models");
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
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
