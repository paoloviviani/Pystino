import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UsageReport } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminReports } from "./AdminReports";

/** The chargeback screen. Its job is to be reconcilable, so what is tested is
 *  that it says which period and timezone it covers, that it declares how much
 *  of the figure is measured, and that the filters reach the API unmangled. */

function report(overrides: Partial<UsageReport> = {}): UsageReport {
  const totals = {
    key: null,
    label: "total",
    split_key: null,
    split_label: null,
    requests: 100,
    prompt_tokens: 8000,
    completion_tokens: 2000,
    total_tokens: 10000,
    images: 0,
    searches: 0,
    own_searches: 0,
    cost: "42.500000000000",
    native_cost: null,
    native_currency: null,
    estimated_requests: 0,
    unavailable_requests: 0,
    estimated_disconnected_requests: 0,
    via_alias_requests: 0,
  };
  return {
    period: {
      label: "2026-08",
      kind: "month",
      start: "2026-07-31T22:00:00Z",
      end: "2026-08-31T22:00:00Z",
      timezone: "Europe/Rome",
    },
    group_by: "group",
    split_by: null,
    currency: "EUR",
    rows: [{ ...totals, key: "g1", label: "research" }],
    totals,
    disclosures: [],
    reconciliation: [],
    ...overrides,
  };
}

function routes(body: UsageReport, seen: string[] = []) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    let payload: unknown = [];
    if (url.includes("/reports/usage")) payload = body;
    else if (url.includes("/api/admin/groups")) {
      payload = [
        { id: "g1", name: "research", description: null, source: "idp", is_active: true, member_count: 2, models: [] },
      ];
    } else if (url.includes("/api/admin/models")) {
      payload = [];
    }
    return jsonResponse(payload);
  });
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(2026, 7, 15));
  // The disclosures remember their open-or-closed choice per page; one
  // test's expanded section must not leak into the next test's render.
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AdminReports", () => {
  it("names the period and the timezone it was computed in", async () => {
    // "August" is ambiguous until you say in which timezone, and a report whose
    // boundaries are invisible cannot be reconciled against anything.
    vi.stubGlobal("fetch", routes(report()));
    renderScreen(<AdminReports />);

    await waitFor(() => expect(screen.getByText(/Spend · 2026-08/)).toBeInTheDocument());
    expect(screen.getByText(/Europe\/Rome calendar period/)).toBeInTheDocument();
  });

  it("declares that every request was measured when it was", async () => {
    vi.stubGlobal("fetch", routes(report()));
    renderScreen(<AdminReports />);

    await waitFor(() => expect(screen.getByText("100%")).toBeInTheDocument());
    expect(screen.getByText(/exact usage from the provider/)).toBeInTheDocument();
  });

  it("says what share of the figure is inferred", async () => {
    const inexact = report();
    inexact.totals = { ...inexact.totals, estimated_requests: 20, unavailable_requests: 5 };
    inexact.disclosures = ["20 of 100 requests have estimated token counts."];
    vi.stubGlobal("fetch", routes(inexact));
    renderScreen(<AdminReports />);

    await waitFor(() => expect(screen.getByText("75%")).toBeInTheDocument());
    // The caveat collapses to one line; the note itself is behind the toggle.
    const toggle = screen.getByRole("button", { name: /1 note about these figures/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByRole("note")).toHaveLength(1);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const [, caveat] = screen.getAllByRole("note");
    expect(caveat).toHaveTextContent("20 of 100 requests have estimated token counts.");
    expect(caveat).toBeVisible();
  });

  it("remembers this page's open-or-closed choice", async () => {
    vi.stubGlobal("fetch", routes(report({ disclosures: ["A first note.", "A second note."] })));
    const first = renderScreen(<AdminReports />);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(await screen.findByRole("button", { name: /2 notes/ }));
    expect(window.localStorage.getItem("llmp.console.disclosures.adminReports")).toBe("true");
    first.unmount();

    renderScreen(<AdminReports />);
    expect(await screen.findByRole("button", { name: /2 notes/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("sends the chosen breakdown and filters to the API", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(report(), seen));
    renderScreen(<AdminReports />);

    await waitFor(() => expect(screen.getByText(/Spend · 2026-08/)).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Breakdown"), "model");
    await user.selectOptions(screen.getByLabelText("Group"), "g1");

    await waitFor(() =>
      expect(
        seen.some((url) => url.includes("group_by=model") && url.includes("group_id=g1")),
      ).toBe(true),
    );
  });

  it("defaults to the current month", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(report(), seen));
    renderScreen(<AdminReports />);

    await waitFor(() => expect(seen.some((url) => url.includes("period=2026-08"))).toBe(true));
  });

  it("shows the API's message when the report cannot be run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/reports/usage")) {
          return new Response(JSON.stringify({ error: { message: "'nope' is not a period." } }), {
            status: 400,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
    renderScreen(<AdminReports />);

    // Once for the tables and once for the chart, which reads the same endpoint.
    await waitFor(() =>
      expect(screen.getAllByText("'nope' is not a period.")).toHaveLength(2),
    );
  });

  it("charts the same report endpoint, daily and split by the chosen breakdown", async () => {
    const user = userEvent.setup({ delay: null });
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(report(), seen));
    renderScreen(<AdminReports />);

    const chart = await screen.findByLabelText("Chart breakdown");
    const options = Array.from(chart.querySelectorAll("option")).map((o) => o.value);
    // Admin views may break the chart down by all four.
    expect(options).toEqual(["group", "user", "model", "provider"]);
    await user.selectOptions(chart, "provider");
    await waitFor(() =>
      expect(
        seen.some(
          (url) =>
            url.includes("/api/admin/reports/usage?") &&
            url.includes("group_by=day") &&
            url.includes("split_by=provider"),
        ),
      ).toBe(true),
    );
  });

  it("adds a provider breakdown to the table", async () => {
    const seen: string[] = [];
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes(report(), seen));
    renderScreen(<AdminReports />);
    await user.selectOptions(await screen.findByLabelText("Breakdown"), "provider");
    await waitFor(() =>
      expect(
        seen.some((url) => url.includes("group_by=provider") && !url.includes("split_by")),
      ).toBe(true),
    );
    expect(await screen.findByRole("columnheader", { name: "Provider" })).toBeInTheDocument();
  });
});
