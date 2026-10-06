import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LimitRule } from "../lib/types";
import { MoneyPrecisionProvider } from "@llmp/ui";
import { jsonResponse } from "../test-helpers";
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

function routes(rules: LimitRule[], health: unknown = { windows: [] }) {
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
    else if (url.includes("/api/admin/users")) payload = [];
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
    await user.click(screen.getByRole("button", { name: "Reset" }));

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/This does not change anyone's bill/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/cannot be scheduled/)).toBeInTheDocument();
  });

  it("will not submit a reset without a reason", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.stubGlobal("fetch", routes([rule()]));
    renderScreen(<AdminQuotas />);

    await waitFor(() => expect(screen.getByText("€31.40 of €50.00")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Reset" }));

    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Reset consumption" });
    expect(confirm).toBeDisabled();

    await user.type(within(dialog).getByLabelText("Reason"), "grant extension approved");
    expect(confirm).toBeEnabled();
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
});
