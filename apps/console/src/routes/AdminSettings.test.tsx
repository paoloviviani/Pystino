import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RedirectUri } from "./AdminSettings";

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
