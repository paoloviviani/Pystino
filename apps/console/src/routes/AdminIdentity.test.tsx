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
    admin_groups: [],
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
  it("shows the policy in force, and where it came from", async () => {
    mockGet(policyFixture());
    renderScreen(<AdminIdentity />);
    expect(await screen.findByText("from the environment")).toBeInTheDocument();
    // The provisioning checkbox is on, because the environment's default is on.
    expect(screen.getByRole("checkbox", { name: /create an account on first sign-in/i })).toBeChecked();
  });

  it("reports a console decision as its own", async () => {
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
    expect(await screen.findByText("set in the console")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /create an account on first sign-in/i })).not.toBeChecked();
    // The stored reason is on the record, not hidden behind a status call.
    expect(await screen.findByText("approval required")).toBeInTheDocument();
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

  it("sends the decision, not the blank form", async () => {
    const user = userEvent.setup({ delay: null });
    mockGet(
      policyFixture({
        groups_claim: "roles",
        admin_groups: ["admins"],
        group_mappings: [{ idp: "platform-admins", local: "admins" }],
      }),
    );
    renderScreen(<AdminIdentity />);
    await screen.findByText("from the environment");

    // Saving with nothing changed is not a decision; the button says so.
    const save = screen.getByRole("button", { name: "Save policy" });
    expect(save).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: /create an account on first sign-in/i }));
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/oidc/policy",
      expect.objectContaining({ method: "PUT" }),
    ));
    const call = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes("/oidc/policy") && (init as RequestInit).method === "PUT",
    );
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    // The form was seeded from the policy in force: what is sent carries the
    // values it showed, plus the one decision that changed.
    expect(body.groups_claim).toBe("roles");
    expect(body.admin_groups).toEqual(["admins"]);
    expect(body.auto_provision).toBe(false);
  });
});
