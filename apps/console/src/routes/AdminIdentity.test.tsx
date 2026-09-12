import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OidcPolicy } from "../lib/types";
import { ProvisioningPolicySection as AdminIdentity } from "./AdminIdentity";

/**
 * The identity policy screen.
 *
 * What matters here is not the form — it is that the screen edits *decisions*,
 * not settings: it opens showing what is in force (and where each value came
 * from), disables Save until something differs, and the unknown-user rule is
 * unreachable while provisioning is on, because storing a knob that cannot
 * apply reads as a bug.
 */

function policyFixture(overrides: Partial<OidcPolicy> = {}): OidcPolicy {
  return {
    auto_provision: true,
    unknown_user_policy: "refuse",
    groups_claim: "groups",
    group_mappings: [],
    source: "environment",
    sources: {},
    configured: null,
    propagation_seconds: 10,
    ...overrides,
  };
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

function renderScreen(element: ReactElement) {
  // Fresh cache per render: the policy query must be this test's fetch, not a
  // previous test's answer.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockGet(policy: OidcPolicy) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/admin/oidc/policy")) {
      return new Response(JSON.stringify(policy), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
}

describe("AdminIdentity", () => {
  it("seeds the form from the policy in force", async () => {
    mockGet(policyFixture());
    renderScreen(<AdminIdentity />);
    // The provisioning checkbox is on, because the environment's default is on.
    expect(
      await screen.findByRole("checkbox", { name: /create an account on first sign-in/i }),
    ).toBeChecked();
  });

  it("seeds a console decision as its own", async () => {
    mockGet(
      policyFixture({
        auto_provision: false,
        unknown_user_policy: "create_inactive",
        source: "console",
        sources: { auto_provision: "console", unknown_user_policy: "console" },
        configured: {
          reason: "approval required",
          changed_at: "2026-09-02T12:00:00Z",
          changed_by: "ops@example.org",
        },
      }),
    );
    renderScreen(<AdminIdentity />);
    expect(
      await screen.findByRole("checkbox", { name: /create an account on first sign-in/i }),
    ).not.toBeChecked();
    // And the rule seeded with it is the one the decision named.
    expect(
      await screen.findByLabelText(/first-time sign-in while provisioning is off/i),
    ).toHaveValue("create_inactive");
  });

  it("offers the unknown-user rule only while provisioning is off", async () => {
    const user = userEvent.setup({ delay: null });
    mockGet(policyFixture());
    renderScreen(<AdminIdentity />);

    await screen.findByRole("checkbox", { name: /create an account on first sign-in/i });
    // On: no rule to choose, because it could not apply.
    expect(
      screen.queryByLabelText(/first-time sign-in while provisioning is off/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /create an account on first sign-in/i }));
    expect(
      await screen.findByLabelText(/first-time sign-in while provisioning is off/i),
    ).toBeInTheDocument();
  });

  it("saves each setting as its own decision", async () => {
    const user = userEvent.setup({ delay: null });
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(policyFixture()), { status: 200 }),
    );
    mockGet(policyFixture());
    renderScreen(<AdminIdentity />);
    await screen.findByRole("checkbox", { name: /create an account on first sign-in/i });

    // Toggling the checkbox is the save: no form, no second button.
    await user.click(screen.getByRole("checkbox", { name: /create an account on first sign-in/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/oidc/policy",
      expect.objectContaining({ method: "PUT" }),
    ));
    const call = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes("/oidc/policy") && (init as RequestInit).method === "PUT",
    );
    // One knob, one decision — the body carries that knob and nothing else,
    // which is what the per-field rows in the policy history mean.
    expect(JSON.parse((call?.[1] as RequestInit).body as string)).toEqual({
      auto_provision: false,
    });
  });
});
