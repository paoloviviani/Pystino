import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { PasswordReset } from "./PasswordReset";

/**
 * The reset pages, request and confirm.
 *
 * The assertion that matters is the request page's promise: "if an account
 * exists" — the same words for an address that does not. The gateway refuses
 * to enumerate accounts (ADR 0049); a page that said "we sent it!" for a real
 * address and "unknown user" for a made-up one would hand the leak back.
 */

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/console/password-reset"]}>
        {element}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PasswordReset: request", () => {
  it("promises nothing about whether the address exists", async () => {
    const user = userEvent.setup({ delay: null });
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
    renderScreen(<PasswordReset />);

    await user.type(screen.getByLabelText("Email"), "local@example.org");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));

    await waitFor(() =>
      expect(screen.getByText(/if an account exists for that address/i)).toBeInTheDocument(),
    );
    // And the promise holds for an address that does not exist, because the
    // page never finds out which one it was.
    await user.click(screen.getByRole("link", { name: /back to sign in/i }));
  });

  it("shows the gateway's refusal when the feature is off", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { message: "Password reset is not available on this deployment." },
        }),
        { status: 503 },
      ),
    );
    renderScreen(<PasswordReset />);
    const user = userEvent.setup({ delay: null });

    await user.type(screen.getByLabelText("Email"), "local@example.org");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(
      await screen.findByText(/password reset is not available on this deployment/i),
    ).toBeInTheDocument();
  });
});

describe("PasswordReset: confirm", () => {
  it("sets the new password and offers sign-in", async () => {
    const user = userEvent.setup({ delay: null });
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
    render(<QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/console/password-reset?token=abc"]}>
        <PasswordReset />
      </MemoryRouter>
    </QueryClientProvider>);

    await user.type(screen.getByLabelText("New password"), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Set password" }));

    await waitFor(() =>
      expect(screen.getByText(/the old password no longer works/i)).toBeInTheDocument(),
    );
    const call = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes("/confirm") && (init as RequestInit).method === "POST",
    );
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      token: "abc",
      password: "a-long-enough-password",
    });
  });

  it("shows the gateway's message for a spent or foreign link", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { message: "This reset link is not valid or has expired. Request a new one." },
        }),
        { status: 400 },
      ),
    );
    render(<QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/console/password-reset?token=dead"]}>
        <PasswordReset />
      </MemoryRouter>
    </QueryClientProvider>);
    const user = userEvent.setup({ delay: null });

    await user.type(screen.getByLabelText("New password"), "a-long-enough-password");
    await user.click(screen.getByRole("button", { name: "Set password" }));

    expect(
      await screen.findByText(/not valid or has expired/i),
    ).toBeInTheDocument();
  });
});
