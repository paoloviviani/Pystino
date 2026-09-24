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
    expect(assign).toHaveBeenCalledWith("/auth/login?next=%2F&provider=partner");
    expect(screen.getByRole("button", { name: "Sign in with corp" })).toBeTruthy();
  });

  it("reports a deployment with no provider rather than showing a password form", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS([])));
    renderLogin();
    expect(await screen.findByText("No identity provider is enabled")).toBeTruthy();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });
});
