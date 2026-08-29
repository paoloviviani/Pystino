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

const METHODS = (local: boolean, oidc: boolean) =>
  new Response(JSON.stringify({ local, oidc }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

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
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(false, true)));
    renderLogin();
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/auth/login?next=%2F"));
  });

  it("shows the password form when local is enabled, alongside an SSO link when both are on", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(true, true)));
    renderLogin();
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in with SSO" })).toBeInTheDocument();
  });

  it("hides the SSO link when local is the only method", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => METHODS(true, false)));
    renderLogin();
    await waitFor(() => expect(screen.getByLabelText("Email")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Sign in with SSO" })).not.toBeInTheDocument();
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
    renderLogin("/?next=/admin/quotas", "/admin/quotas");
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
