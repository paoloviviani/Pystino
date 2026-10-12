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
 * from), saves each choice as its own row in the policy history, and never
 * saves a group gate without saying how many people it would cut off.
 */

function policyFixture(overrides: Partial<OidcPolicy> = {}): OidcPolicy {
  return {
    auto_provision: true,
    unknown_user_policy: "refuse",
    join_mode: "open",
    required_groups: [],
    group_fallback: "refuse",
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

function mockRoutes({ policy, groups }: { policy: OidcPolicy; groups?: { id: string; name: string }[] }) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/admin/oidc/policy") && (init?.method ?? "GET") === "PUT") {
      return new Response(JSON.stringify(policy), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/admin/oidc/policy/preview")) {
      const params = new URL(url, "http://x").searchParams;
      const refused = params.get("required_groups")?.includes("cut-off") ? 2 : 0;
      return new Response(
        JSON.stringify({ active_users: 5, would_be_refused: refused, admins_exempt: 1 }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (url.includes("/api/admin/groups")) {
      return new Response(
        JSON.stringify({ items: groups ?? [], total: (groups ?? []).length }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (url.includes("/api/admin/oidc/policy")) {
      return new Response(JSON.stringify(policy), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
}

const putBodies = () =>
  fetchMock.mock.calls
    .filter(([url, init]) => String(url).includes("/oidc/policy") && (init as RequestInit).method === "PUT")
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));

describe("AdminIdentity", () => {
  it("seeds the radio group from the policy in force", async () => {
    mockRoutes({ policy: policyFixture({ join_mode: "invite" }) });
    renderScreen(<AdminIdentity />);
    expect(await screen.findByRole("radio", { name: /^Invite/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Open/ })).not.toBeChecked();
  });

  it("says where each answer came from", async () => {
    mockRoutes({
      policy: policyFixture({
        join_mode: "approval",
        sources: { join_mode: "console" },
      }),
    });
    renderScreen(<AdminIdentity />);
    expect(await screen.findByText(/join mode set here/i)).toBeInTheDocument();
  });

  it("shows the group controls only in group mode", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({
      policy: policyFixture({ join_mode: "group", required_groups: [] }),
      groups: [
        { id: "g1", name: "staff" },
        { id: "g2", name: "ops" },
      ],
    });
    renderScreen(<AdminIdentity />);
    expect(await screen.findByRole("radio", { name: /^Group/ })).toBeChecked();
    expect(await screen.findByRole("checkbox", { name: "staff" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "ops" })).toBeInTheDocument();
    expect(
      await screen.findByLabelText(/someone without a required group/i),
    ).toHaveValue("refuse");

    // Switching away hides them; the save is the radio click itself.
    await user.click(screen.getByRole("radio", { name: /^Invite/ }));
    await waitFor(() => expect(putBodies()).toContainEqual({ join_mode: "invite" }));
    expect(screen.queryByRole("checkbox", { name: "staff" })).not.toBeInTheDocument();
  });

  it("refuses to enter group mode with no groups picked", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({ policy: policyFixture(), groups: [] });
    renderScreen(<AdminIdentity />);
    await screen.findByRole("radio", { name: /^Open/ });
    await user.click(screen.getByRole("radio", { name: /^Group/ }));
    expect(
      await screen.findByText(/pick at least one required group first/i),
    ).toBeInTheDocument();
    expect(putBodies()).toEqual([]);
  });

  it("previews a group gate that would cut people off, and saves only on confirm", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({
      policy: policyFixture(),
      groups: [
        { id: "g1", name: "cut-off" },
        { id: "g2", name: "staff" },
      ],
    });
    renderScreen(<AdminIdentity />);
    await screen.findByRole("radio", { name: /^Open/ });

    // Pick the groups first (they persist in local state), then the mode.
    await user.click(screen.getByRole("radio", { name: /^Group/ }));
    expect(
      await screen.findByText(/pick at least one required group first/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "cut-off" }));

    const warning = await screen.findByText(/will lose access/i);
    expect(warning).toBeInTheDocument();
    expect(putBodies()).toEqual([]);

    await user.click(screen.getByRole("button", { name: /save anyway/i }));
    await waitFor(() =>
      expect(putBodies()).toEqual([
        { join_mode: "group", required_groups: ["cut-off"], group_fallback: "refuse" },
      ]),
    );
  });

  it("saves group mode straight away when nobody would be refused", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({
      policy: policyFixture(),
      groups: [{ id: "g2", name: "staff" }],
    });
    renderScreen(<AdminIdentity />);
    await screen.findByRole("radio", { name: /^Open/ });
    await user.click(screen.getByRole("radio", { name: /^Group/ }));
    expect(
      await screen.findByText(/pick at least one required group first/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "staff" }));
    await waitFor(() =>
      expect(putBodies()).toEqual([
        { join_mode: "group", required_groups: ["staff"], group_fallback: "refuse" },
      ]),
    );
  });

  it("saves the fallback as its own decision", async () => {
    const user = userEvent.setup({ delay: null });
    mockRoutes({
      policy: policyFixture({ join_mode: "group", required_groups: ["staff"] }),
      groups: [{ id: "g2", name: "staff" }],
    });
    renderScreen(<AdminIdentity />);
    const select = await screen.findByLabelText(/someone without a required group/i);
    await user.selectOptions(select, "approval");
    await waitFor(() =>
      expect(putBodies()).toEqual([
        { join_mode: "group", required_groups: ["staff"], group_fallback: "approval" },
      ]),
    );
  });

  it("still opens showing the legacy view for an old row", async () => {
    // A deployment upgraded mid-flight reads derived values; the screen
    // speaks join modes either way.
    mockRoutes({
      policy: policyFixture({
        join_mode: "approval",
        auto_provision: false,
        unknown_user_policy: "create_inactive",
      }),
    });
    renderScreen(<AdminIdentity />);
    expect(await screen.findByRole("radio", { name: /^Approval/ })).toBeChecked();
  });
});
