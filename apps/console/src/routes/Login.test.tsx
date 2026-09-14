import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Login } from "./Login";

/**
 * What this page must get right is the *choice*, not the form: which way in
 * this deployment offers decides everything it renders, and choosing wrongly
 * either hides a working password login behind a redirect or sends a
 * local-only deployment off to an identity provider that does not exist.
 */

function renderLogin(route = "/", next?: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          {/* Where `next` lands after a successful sign-in. A real deployment
              has the whole console here; one marker route is enough to prove
              the navigation happened and went where it was told. */}
          {next ? <Route path={next} element={<div>the next page</div>} /> : null}
          <Route path="*" element={<Login />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const METHODS = (local: boolean, oidc: boolean, providers: string[] = []) =>
  new Response(
    JSON.stringify({
      local,
      oidc,
      providers: providers.map((name) => ({ name, issuer: `https://${name}.test` })),
    }),
    {
      status: 200,
      headers: { "content-type": "application/json" },
    },
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
  it("auto-redirects when OIDC is the only method — the behaviour every deployment had before local auth", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(false, true, ["keycloak"])));
    renderLogin();
    // One provider and no local form: straight there, naming it (ADR 0051).
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/auth/login?next=%2F&provider=keycloak"),
    );
  });

  it("shows the password form when local is enabled, alongside an SSO link when both are on", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(true, true, ["keycloak"])));
    renderLogin();
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in with keycloak" })).toBeInTheDocument();
  });

  it("hides the SSO link when local is the only method", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(true, false)));
    renderLogin();
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Sign in with/ })).not.toBeInTheDocument();
  });

  it("signs in and navigates to `next` rather than always to the overview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/auth/methods")) return METHODS(true, false);
        // The gateway sets the session cookie on this response; jsdom does not
        // need it to prove the navigation happened.
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    renderLogin("/?next=/console/admin/quotas", "/admin/quotas");
    const user = userEvent.setup({ delay: null });
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Email"), "root@local");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    // The navigation is the observable contract: the reader left this page
    // for the one `next` named, not for the overview.
    await waitFor(() => expect(screen.getByText("the next page")).toBeInTheDocument());
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
  });

  it("sends the browser itself when `next` belongs to the gateway, not the console", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/auth/methods")) return METHODS(true, false);
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    // The house IdP's round trip for a browser-facing client: the reader
    // signed in to the console while on their way back to `/oauth/authorize`.
    // The router can only render "No such page" for that address; the browser
    // must go there itself, carrying the cookie the sign-in just set.
    const authorize = "/oauth/authorize?client_id=cerea&redirect_uri=https%3A%2F%2Fcerea.test%2Fchat%2Flogin%2Fcallback";
    renderLogin(`/?next=${encodeURIComponent(authorize)}`);
    const user = userEvent.setup({ delay: null });
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Email"), "root@local");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith(authorize));
  });

  it("refuses a `next` that is not a path on this origin rather than following it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/auth/methods")) return METHODS(true, false);
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    // An open redirect: the victim signs in for real, the browser would be
    // sent to the attacker's host holding a fresh session cookie. The page
    // refuses instead of repairing — the console keeps the reader.
    renderLogin("/?next=%2F%2Fevil.test%2Fsteal");
    const user = userEvent.setup({ delay: null });
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Email"), "root@local");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(assign).not.toHaveBeenCalled());
    // Back on the console's front door, not somewhere the value pointed.
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
  });

  it("shows the gateway's message on a refusal instead of a generic one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/auth/methods")) return METHODS(true, false);
        return new Response(
          JSON.stringify({
            error: { message: "Too many failed sign-in attempts. Try again later." },
          }),
          { status: 429, headers: { "content-type": "application/json" } },
        );
      }),
    );
    renderLogin();
    const user = userEvent.setup({ delay: null });
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Email"), "root@local");
    await user.type(screen.getByLabelText("Password"), "whatever-long-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() =>
      expect(screen.getByText("Too many failed sign-in attempts. Try again later.")).toBeVisible(),
    );
  });
});
