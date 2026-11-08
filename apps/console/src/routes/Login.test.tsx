import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Login } from "./Login";

/**
 * OIDC only (ADR 0088, decision D3): what this page must get right is which
 * provider to send the reader to, and never to leave them on a spinner.
 */

function renderLogin(route = "/") {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="*" element={<Login />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const METHODS = (providers: string[]) =>
  new Response(
    JSON.stringify({
      local: false,
      oidc: providers.length > 0,
      providers: providers.map((name) => ({ name, issuer: `https://${name}.test` })),
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );

let assign: ReturnType<typeof vi.fn>;

beforeEach(() => {
  assign = vi.fn();
  Object.defineProperty(window, "location", {
    writable: true,
    value: { ...window.location, assign },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Login", () => {
  it("sends the reader straight to the only provider, carrying next", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(["authelia"])));
    renderLogin("/?next=%2Fquotas");
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/auth/login?next=%2Fquotas&provider=authelia"),
    );
  });

  it("offers one button per provider when there are several, instead of a spinner", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(["corp", "partner"])));
    renderLogin();
    await userEvent.click(await screen.findByRole("button", { name: "Sign in with partner" }));
    expect(assign).toHaveBeenCalledWith("/auth/login?next=%2Fconsole&provider=partner");
    expect(screen.getByRole("button", { name: "Sign in with corp" })).toBeTruthy();
  });

  it("reports a deployment with no provider rather than showing a password form", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS([])));
    renderLogin();
    expect(await screen.findByText("No identity provider is enabled")).toBeTruthy();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });
  // The console renders this page in place of whatever route the reader opened
  // once the session is gone, so with no ?next it returns them there. It never
  // falls back to the origin root: on a kit "/" is the chat app.
  const at = (pathname: string, search = "") =>
    Object.defineProperty(window, "location", {
      writable: true,
      value: { ...window.location, pathname, search, assign },
    });

  it("returns to the console page the reader opened when no next is given", async () => {
    at("/console/admin/quotas", "?tab=rules");
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(["authelia"])));
    renderLogin("/admin/quotas?tab=rules");
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/auth/login?next=%2Fconsole%2Fadmin%2Fquotas%3Ftab%3Drules&provider=authelia",
      ),
    );
  });

  it("never returns to the origin root, which on a kit is the chat app", async () => {
    at("/console/login");
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(["authelia"])));
    renderLogin("/login?next=%2F");
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/auth/login?next=%2Fconsole&provider=authelia"),
    );
  });

  it("ignores an unsafe next and returns to the console instead", async () => {
    at("/console/login");
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(["authelia"])));
    renderLogin("/login?next=%2F%2Fevil.test");
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/auth/login?next=%2Fconsole&provider=authelia"),
    );
  });
});
