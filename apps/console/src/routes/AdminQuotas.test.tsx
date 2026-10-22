import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LimitRule } from "../lib/types";
import { MoneyPrecisionProvider } from "@llmp/ui";
import { jsonResponse, chooseRowAction } from "../test-helpers";
import { AdminQuotas } from "./AdminQuotas";

/**
 * The quota screen is where a misleading number has consequences: someone reads
 * it and decides whether to raise a cap or tell a colleague to wait. So what is
 * tested is what it says, not how it looks — above all that "the counter is
 * unreachable" never renders as "plenty of budget left", and that a reset is
 * described honestly.
 */

function rule(overrides: Partial<LimitRule> = {}): LimitRule {
  return {
    id: "r1",
    name: "research monthly",
    scope: "group",
    scope_id: "g1",
    metric: "cost",
    applies_to: "aggregate",
    window_seconds: null,
    period: "month",
    window_label: "month",
    limit_value: "50.000000000000",
    is_active: true,
    current_value: "31.400000000000",
    last_reset_at: null,
    ...overrides,
  };
}

function routes(rules: LimitRule[], health: unknown = { windows: [] }, users: unknown[] = []) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    let payload: unknown = [];
    if (url.includes("/api/admin/limits") && url.includes("/resets")) payload = [];
    else if (url.includes("/api/admin/quota/health")) payload = health;
    else if (url.includes("/api/admin/quota/reconcile") && method === "POST")
      payload = { reconciled: [], corrected: 1 };
    else if (url.includes("/api/admin/limits") && method === "GET") payload = rules;
    else if (url.includes("/api/admin/groups")) payload = [{ id: "g1", name: "research", description: null, source: "idp", is_active: true, member_count: 3, models: [] }];
    else if (url.includes("/api/admin/users")) payload = users;
    return jsonResponse(payload);
  });
}

function renderScreen(element: ReactElement, exactMoney = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MoneyPrecisionProvider exact={exactMoney}>
        <MemoryRouter>{element}</MemoryRouter>
      </MoneyPrecisionProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(2026, 7, 15));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AdminQuotas", () => {
  it("shows consumption against the limit, in money", async () => {
    vi.stubGlobal("fetch", routes([rule()]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€31.40 of €50.00")).toBeInTheDocument());
  });

  it("rounds a sub-milli figure rather than printing the ledger at you", async () => {
    // The complaint this fixes: "€7.32012 of €10.00" on a screen someone reads
    // to decide whether to raise a cap.
    vi.stubGlobal(
      "fetch",
      routes([rule({ current_value: "7.320119870000", limit_value: "10.000000000000" })]),
    );
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€7.32 of €10.00")).toBeInTheDocument());
  });

  it("shows every digit when the reader has asked for exact figures", async () => {
    // The escape hatch, reached from the identity menu. This screen reads the
    // preference by hand because its figures sit inside a phrase, so the wiring
    // is worth pinning rather than assuming.
    vi.stubGlobal(
      "fetch",
      routes([rule({ current_value: "7.320119870000", limit_value: "10.000000000000" })]),
    );
    renderScreen(<AdminQuotas />, true);

    await waitFor(() =>
      expect(screen.getByText("€7.32011987 of €10.00")).toBeInTheDocument(),
    );
  });

  it("names the window the rule uses", async () => {
    vi.stubGlobal("fetch", routes([rule()]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("per month")).toBeInTheDocument());
  });

  it("puts a rolling window in words rather than seconds", async () => {
    // The API reports raw seconds because that is what it stores. "per 21600s"
    // is not something to put in front of a person.
    vi.stubGlobal(
      "fetch",
      routes([rule({ period: null, window_seconds: 21600, window_label: "21600s" })]),
    );
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("per 6 hours")).toBeInTheDocument());
  });

  it("reads a day-long window as 24 hours", async () => {
    vi.stubGlobal(
      "fetch",
      routes([rule({ period: null, window_seconds: 86400, window_label: "86400s" })]),
    );
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("per 24 hours")).toBeInTheDocument());
  });

  it("flags a rule that is over its cap", async () => {
    // A rule at 200% still showed a green "Active", which is true and useless:
    // what the row means is that requests are being refused right now.
    vi.stubGlobal("fetch", routes([rule({ current_value: "62.000000000000" })]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("Over budget")).toBeInTheDocument());
    expect(screen.queryByText("Active")).not.toBeInTheDocument();
  });

  it("warns before a budget is spent, not only after", async () => {
    vi.stubGlobal("fetch", routes([rule({ current_value: "45.000000000000" })]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("Nearly spent")).toBeInTheDocument());
  });

  it("an unreachable counter is not reported as over budget", async () => {
    vi.stubGlobal("fetch", routes([rule({ current_value: null })]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("counter unavailable")).toBeInTheDocument());
    expect(screen.queryByText("Over budget")).not.toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("says the counter is unavailable rather than showing zero", async () => {
    // The distinction that matters: `current_value: null` means the counter
    // store could not be reached. Rendering an empty bar would read as "plenty
    // of budget left" and is exactly the wrong thing to tell someone.
    vi.stubGlobal("fetch", routes([rule({ current_value: null })]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("counter unavailable")).toBeInTheDocument());
    expect(screen.queryByText(/€0\.00 of/)).not.toBeInTheDocument();
  });

  it("marks a rule that has been reset", async () => {
    vi.stubGlobal("fetch", routes([rule({ last_reset_at: "2026-08-10T09:00:00Z" })]));
    renderScreen(<AdminQuotas />);

    // Scoped to the badge: the dialogs render (closed) in the same tree, so
    // "Reset consumption" is also present and matches the same pattern.
    await waitFor(() =>
      expect(screen.getByText(/^Reset .+/, { selector: "span" })).toBeInTheDocument(),
    );
  });

  it("says a reset does not change the bill, before you confirm it", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal("fetch", routes([rule()]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€31.40 of €50.00")).toBeInTheDocument());
    await chooseRowAction(user, "Reset");

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/This does not change anyone's bill/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/cannot be scheduled/)).toBeInTheDocument();
  });

  it("submits a reset with no reason: the field is optional", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchMock = routes([rule()]);
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€31.40 of €50.00")).toBeInTheDocument());
    await chooseRowAction(user, "Reset");

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Reason (optional)")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Reset consumption" });
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([url, init]) => String(url).endsWith("/reset") && init?.method === "POST",
      );
      expect(post).toBeDefined();
      expect(JSON.parse(String(post![1]!.body))).toEqual({ reason: "" });
    });
  });

  it("sends the reason when one is given", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const fetchMock = routes([rule()]);
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€31.40 of €50.00")).toBeInTheDocument());
    await chooseRowAction(user, "Reset");

    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (optional)"), "grant extension approved");
    await user.click(within(dialog).getByRole("button", { name: "Reset consumption" }));
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([url, init]) => String(url).endsWith("/reset") && init?.method === "POST",
      );
      expect(JSON.parse(String(post![1]!.body))).toEqual({ reason: "grant extension approved" });
    });
  });

  it("says plainly when nothing is capped", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminQuotas />);

    await waitFor(() =>
      expect(screen.getByText("No quota rules. Nothing is capped.")).toBeInTheDocument(),
    );
  });

  describe("quota health", () => {
    const drifted = {
      windows: [
        {
          rule_id: "r1",
          rule_name: "research monthly",
          scope: "group",
          scope_id: "g1",
          metric: "cost",
          window_label: "month",
          is_active: true,
          window_id: "p2026-10",
          counter_value: "0.379800000000",
          ledger_total: "0.283200000000",
          difference: "0.096600000000",
          counter_ttl_seconds: 86400,
          stale_in_progress: 2,
        },
      ],
      rebuild_lock_present: false,
      rebuild_lock_ttl_seconds: null,
    };

    it("shows counter, ledger and the difference between them", async () => {
      vi.stubGlobal("fetch", routes([rule()], drifted));
      renderScreen(<AdminQuotas />);

      expect(await screen.findByText("€0.38")).toBeInTheDocument();
      expect(screen.getByText("€0.283")).toBeInTheDocument();
      expect(screen.getByText("€0.097")).toBeInTheDocument();
    });

    it("does not call an unreadable counter zero", async () => {
      const unreadable = {
        ...drifted,
        windows: [{ ...drifted.windows[0], counter_value: null, difference: null }],
      };
      vi.stubGlobal("fetch", routes([rule()], unreadable));
      renderScreen(<AdminQuotas />);

      expect((await screen.findAllByText("counter unavailable")).length).toBeGreaterThan(0);
    });

    it("reconciles on request", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const fetchMock = routes([rule()], drifted);
      vi.stubGlobal("fetch", fetchMock);
      renderScreen(<AdminQuotas />);

      await user.click(await screen.findByRole("button", { name: "Reconcile" }));

      await waitFor(() =>
        expect(
          fetchMock.mock.calls.some(
            ([url, init]) =>
              String(url).includes("/api/admin/quota/reconcile") && init?.method === "POST",
          ),
        ).toBe(true),
      );
    });
  });

  describe("per-member rules", () => {
    const member = { id: "u1", email: "ada@example.org", display_name: "Ada" };
    const share = () =>
      rule({
        id: "r2",
        name: "member share",
        limit_value: "50.000000000000",
        current_value: "12.500000000000",
        applies_to: "each_member",
      });

    it("offers the choice for a group scope, and nowhere else", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.stubGlobal("fetch", routes([rule()]));
      renderScreen(<AdminQuotas />);
      await user.click(await screen.findByRole("button", { name: "New rule" }));

      // Group is the default scope, and the choice is there.
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByLabelText("Applies to")).toBeInTheDocument();
      expect(
        within(within(dialog).getByLabelText("Applies to")).getByRole("option", {
          name: "Each member",
        }),
      ).toBeInTheDocument();

      await user.selectOptions(within(dialog).getByLabelText("Scope"), "user");
      expect(within(dialog).queryByLabelText("Applies to")).toBeNull();

      // Everyone has its own choice: the total, or each user separately.
      await user.selectOptions(within(dialog).getByLabelText("Scope"), "global");
      const appliesTo = within(dialog).getByLabelText("Applies to");
      expect(within(appliesTo).getByRole("option", { name: "Everyone's total" })).toBeInTheDocument();
      expect(
        within(appliesTo).getByRole("option", { name: "Each user separately" }),
      ).toBeInTheDocument();
      expect(within(appliesTo).queryByRole("option", { name: "Each member" })).toBeNull();
    });

    it("sends each_user when creating a rule for everyone's own ceiling", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const fetchMock = routes([rule()]);
      vi.stubGlobal("fetch", fetchMock);
      renderScreen(<AdminQuotas />);
      await user.click(await screen.findByRole("button", { name: "New rule" }));

      const dialog = await screen.findByRole("dialog");
      await user.selectOptions(within(dialog).getByLabelText("Scope"), "global");
      await user.selectOptions(within(dialog).getByLabelText("Applies to"), "each_user");
      await user.click(within(dialog).getByRole("button", { name: "Create" }));

      await waitFor(() => {
        const post = fetchMock.mock.calls.find(
          ([url, init]) =>
            String(url).endsWith("/api/admin/limits") && init?.method === "POST",
        );
        expect(post).toBeDefined();
        expect(JSON.parse(String(post![1]!.body))).toMatchObject({
          scope: "global",
          scope_id: null,
          applies_to: "each_user",
        });
      });
    });

    it("prints a per-user rule as per user with no single figure", async () => {
      vi.stubGlobal(
        "fetch",
        routes([rule({ id: "r3", name: "personal ceiling", scope: "global", scope_id: null, applies_to: "each_user", current_value: null })]),
      );
      renderScreen(<AdminQuotas />);

      await waitFor(() => expect(screen.getByText("personal ceiling")).toBeInTheDocument());
      expect(screen.getByText("per month per user")).toBeInTheDocument();
      expect(screen.getByText("per user · see health")).toBeInTheDocument();
      expect(screen.queryByText("counter unavailable")).toBeNull();
    });

    it("offers the override checkbox for one person only, and sends it", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const fetchMock = routes([rule()], { windows: [] }, [
        { id: "u1", email: "ada@example.test", display_name: "Ada", subject: "s1" },
      ]);
      vi.stubGlobal("fetch", fetchMock);
      renderScreen(<AdminQuotas />);
      await user.click(await screen.findByRole("button", { name: "New rule" }));

      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).queryByLabelText("Overrides everyone's ceiling")).toBeNull();
      await user.selectOptions(within(dialog).getByLabelText("Scope"), "global");
      expect(within(dialog).queryByLabelText("Overrides everyone's ceiling")).toBeNull();

      await user.selectOptions(within(dialog).getByLabelText("Scope"), "user");
      expect(within(dialog).getByText(/replaces every "each user separately" rule/)).toBeInTheDocument();
      await user.selectOptions(await within(dialog).findByLabelText("User"), "u1");
      await user.click(within(dialog).getByLabelText("Overrides everyone's ceiling"));
      await user.click(within(dialog).getByRole("button", { name: "Create" }));

      await waitFor(() => {
        const post = fetchMock.mock.calls.find(
          ([url, init]) =>
            String(url).endsWith("/api/admin/limits") && init?.method === "POST",
        );
        expect(post).toBeDefined();
        expect(JSON.parse(String(post![1]!.body))).toMatchObject({
          scope: "user",
          scope_id: "u1",
          overrides_each_user: true,
        });
      });
    });

    it("marks an override rule in the list", async () => {
      vi.stubGlobal(
        "fetch",
        routes([rule({ id: "r4", name: "ada higher", scope: "user", scope_id: "u1", overrides_each_user: true })]),
      );
      renderScreen(<AdminQuotas />);

      await waitFor(() => expect(screen.getByText("ada higher")).toBeInTheDocument());
      expect(screen.getByText("Overrides everyone's ceiling")).toBeInTheDocument();
    });

    it("sends applies_to when creating a per-member rule", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const fetchMock = routes([rule()]);
      vi.stubGlobal("fetch", fetchMock);
      renderScreen(<AdminQuotas />);
      await user.click(await screen.findByRole("button", { name: "New rule" }));

      const dialog = await screen.findByRole("dialog");
      await user.selectOptions(within(dialog).getByLabelText("Group"), "g1");
      await user.selectOptions(within(dialog).getByLabelText("Applies to"), "each_member");
      const limit = within(dialog).getByLabelText(/Limit/);
      await user.clear(limit);
      await user.type(limit, "50");
      await user.click(within(dialog).getByRole("button", { name: "Create" }));

      await waitFor(() => {
        const post = fetchMock.mock.calls.find(
          ([url, init]) =>
            String(url).endsWith("/api/admin/limits") && init?.method === "POST",
        );
        expect(post).toBeDefined();
        expect(JSON.parse(String(post![1]!.body))).toMatchObject({
          scope: "group",
          scope_id: "g1",
          applies_to: "each_member",
        });
      });
    });

    it("prints the mode with the limit in the rules list", async () => {
      vi.stubGlobal("fetch", routes([rule(), share()]));
      renderScreen(<AdminQuotas />);

      await waitFor(() => expect(screen.getByText("member share")).toBeInTheDocument());
      const tables = screen.getAllByRole("table");
      const rulesTable = tables[0]!;
      const memberRow = within(rulesTable)
        .getAllByRole("row")
        .find((row) => within(row).queryByText("member share"));
      expect(memberRow).toBeDefined();
      expect(within(memberRow!).getByText("per month per member")).toBeInTheDocument();
    });

    it("says plainly that per-member consumption has no single figure", async () => {
      vi.stubGlobal("fetch", routes([share()]));
      renderScreen(<AdminQuotas />);

      await waitFor(() => expect(screen.getByText("member share")).toBeInTheDocument());
      // Not the "counter unavailable" badge: the counter is fine, there is
      // simply no one number for a per-member rule.
      expect(screen.queryByText("counter unavailable")).toBeNull();
      const tables = screen.getAllByRole("table");
      const memberRow = within(tables[0]!)
        .getAllByRole("row")
        .find((row) => within(row).queryByText("member share"));
      expect(within(memberRow!).getByText("per member · see health")).toBeInTheDocument();
    });

    it("names the member on per-member health rows", async () => {
      vi.stubGlobal(
        "fetch",
        routes(
          [share()],
          {
            windows: [
              {
                rule_id: "r2",
                rule_name: "member share",
                scope: "group",
                scope_id: "g1",
                metric: "cost",
                window_label: "month",
                is_active: true,
                window_id: "p2026-10",
                counter_value: "12.500000000000",
                ledger_total: "12.500000000000",
                difference: "0.000000000000",
                counter_ttl_seconds: 3600,
                stale_in_progress: 0,
                member_id: "u1",
              },
            ],
            rebuild_lock_present: false,
            rebuild_lock_ttl_seconds: null,
          },
          [member],
        ),
      );
      renderScreen(<AdminQuotas />);

      await waitFor(() => expect(screen.getByText("Quota health")).toBeInTheDocument());
      const tables = screen.getAllByRole("table");
      const healthTable = tables[tables.length - 1]!;
      const memberRow = within(healthTable)
        .getAllByRole("row")
        .find((row) => within(row).queryByText("member share"));
      expect(memberRow).toBeDefined();
      expect(memberRow!.textContent).toContain("ada@example.org");
    });
  });
});
