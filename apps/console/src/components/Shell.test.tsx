import { render, screen, waitFor } from "@testing-library/react";
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
 */

function me(overrides: Partial<Me> = {}): Me {
  return {
    id: "u1",
    email: "dave@example.org",
    display_name: "Dave",
    is_admin: true,
    groups: [{ id: "g1", name: "platform-admins", description: null }],
    default_billing_group: { id: "g1", name: "platform-admins", description: null },
    ...overrides,
  };
}

function renderShell(user: Me = me()) {
  return render(
    <MemoryRouter>
      <Shell me={user}>
        <p>content</p>
      </Shell>
    </MemoryRouter>,
  );
}

const assign = vi.fn();

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
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("opens the menu and offers to sign out", async () => {
    const user = userEvent.setup({ delay: null });
    renderShell();

    await user.click(screen.getByRole("button", { name: /Dave/ }));
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByText("dave@example.org")).toBeInTheDocument();
  });

  it("posts to the logout endpoint, and only on the button", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    stubNavigation();
    renderShell();

    await user.click(screen.getByRole("button", { name: /Dave/ }));
    expect(fetchMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(call[0])).toContain("/auth/logout");
    expect(call[1].method).toBe("POST");
  });

  it("leaves the page even when the logout request fails", async () => {
    // The cookie may already be gone, or the network may be down. Staying put
    // on an authenticated-looking page is the one outcome that is not
    // acceptable.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    stubNavigation();
    renderShell();

    await user.click(screen.getByRole("button", { name: /Dave/ }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/auth/login"));
  });

  it("reloads the page rather than routing, so no stale data survives", async () => {
    // Every cached query in this tab was fetched as the previous user. A
    // client-side route change would leave that data in memory.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    stubNavigation();
    renderShell();

    await user.click(screen.getByRole("button", { name: /Dave/ }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/auth/login"));
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup({ delay: null });
    renderShell();

    await user.click(screen.getByRole("button", { name: /Dave/ }));
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument(),
    );
  });

  it("hides admin navigation from a non-administrator", () => {
    renderShell(me({ is_admin: false }));
    expect(screen.queryByRole("link", { name: "Providers" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
  });
});
