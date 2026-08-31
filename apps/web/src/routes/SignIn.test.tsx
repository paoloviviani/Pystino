/**
 * The sign-in screen.
 *
 * This file exists for the same reason Chat.test.tsx does: the failure this
 * screen replaces ("Sign-in is unavailable." where a login should be) was a
 * render-time behaviour no API test could see. What is pinned here: the
 * methods answer decides what renders, the gateway's own words reach the
 * screen on a refused password, and the OIDC-only journey is unchanged.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "./SignIn";

type Respond = (url: string, init: RequestInit | undefined) => Response;

function serve(respond: Respond) {
  const fetchMock = vi.fn(async (url: string | URL, init?: RequestInit) =>
    respond(String(url), init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function methodsResponse(methods: { local: boolean; oidc: boolean }): Response {
  return jsonResponse(200, methods);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SignIn", () => {
  it("shows the password form when local sign-in exists, and signs in", async () => {
    const fetchMock = serve((url) => {
      if (url.endsWith("/api/auth/methods")) return methodsResponse({ local: true, oidc: false });
      if (url.endsWith("/api/auth/local")) return new Response(null, { status: 204 });
      throw new Error(`unexpected fetch: ${url}`);
    });
    const onSignedIn = vi.fn();

    render(<SignIn onSignedIn={onSignedIn} />);

    await userEvent.type(await screen.findByLabelText("Email"), "person@example.org");
    await userEvent.type(screen.getByLabelText("Password"), "good password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledOnce());
    const signInCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/api/auth/local"));
    expect(signInCall).toBeDefined();
    const body = JSON.parse(String(signInCall?.[1]?.body));
    expect(body).toEqual({ email: "person@example.org", password: "good password" });
  });

  it("shows the gateway's own words when the password is wrong", async () => {
    serve((url) => {
      if (url.endsWith("/api/auth/methods")) return methodsResponse({ local: true, oidc: false });
      if (url.endsWith("/api/auth/local"))
        return jsonResponse(401, { detail: "Incorrect email or password." });
      throw new Error(`unexpected fetch: ${url}`);
    });

    render(<SignIn onSignedIn={vi.fn()} />);

    await userEvent.type(await screen.findByLabelText("Email"), "person@example.org");
    await userEvent.type(screen.getByLabelText("Password"), "wrong password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Incorrect email or password.")).toBeInTheDocument();
  });

  it("offers the SSO link beside the form when both doors exist", async () => {
    serve((url) => {
      if (url.endsWith("/api/auth/methods")) return methodsResponse({ local: true, oidc: true });
      throw new Error(`unexpected fetch: ${url}`);
    });

    render(<SignIn onSignedIn={vi.fn()} />);

    await screen.findByLabelText("Email");
    const link = screen.getByRole("link", { name: "Sign in with single sign-on" });
    expect(link.getAttribute("href")).toBe("/chat/auth/login?next=%2F");
  });

  it("keeps the straight-to-provider journey when OIDC is the only door", async () => {
    serve((url) => {
      if (url.endsWith("/api/auth/methods")) return methodsResponse({ local: false, oidc: true });
      throw new Error(`unexpected fetch: ${url}`);
    });

    render(<SignIn onSignedIn={vi.fn()} />);

    // The form must not render — the provider's page is the form. (Whether
    // the navigation itself fired is browser behaviour jsdom cannot spy on:
    // window.location is unforgeable there, and assign() logs instead of
    // navigating. The render decision is what this component owns.)
    await waitFor(() =>
      expect(screen.queryByLabelText("Email")).not.toBeInTheDocument(),
    );
  });
});
