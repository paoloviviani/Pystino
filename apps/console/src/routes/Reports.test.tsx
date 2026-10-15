/**
 * A person's own usage, and the caveats that qualify it.
 *
 * The split this file exists to pin: the numbers are on Overview, the
 * *explanation* of the numbers is here. Both are the same API response; which
 * screen shows which half is the decision.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "../test-helpers";
import type { UsageReport } from "../lib/types";
import { Reports } from "./Reports";

function report(overrides: Partial<UsageReport> = {}): UsageReport {
  return {
    period: {
      kind: "month",
      label: "2026-08",
      start: "2026-08-01",
      end: "2026-09-01",
      timezone: "Europe/Rome",
    },
    currency: "EUR",
    group_by: "model",
    rows: [
      {
        key: "m1",
        label: "gpt-4o",
        requests: 12,
        prompt_tokens: 900,
        completion_tokens: 300,
        total_tokens: 1200,
        images: 0,
        searches: 0,
        own_searches: 0,
        cost: "0.003",
        native_cost: null,
        native_currency: null,
        estimated_requests: 0,
        unavailable_requests: 0,
      },
    ],
    totals: {
      key: null,
      label: "Total",
      requests: 12,
      prompt_tokens: 900,
      completion_tokens: 300,
      total_tokens: 1200,
      images: 0,
      searches: 0,
      own_searches: 0,
      cost: "3.250000000000",
      native_cost: null,
      native_currency: null,
      estimated_requests: 0,
      unavailable_requests: 0,
    },
    disclosures: [],
    ...overrides,
  };
}

function respondWith(body: UsageReport) {
  return vi.fn(async (_input: RequestInfo | URL) => jsonResponse(body));
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Reports />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  // The disclosures remember their open-or-closed choice per page; one
  // test's expanded section must not leak into the next test's render.
  window.localStorage.clear();
});

describe("Reports", () => {
  it("collapses the caveats to one line, and shows them verbatim on expand", async () => {
    // Wording a caveat twice — once in the API, once here — is how the two end
    // up disagreeing about what the number means.
    const note = "9 request(s) are charged from our price table: their provider reported none.";
    const second = "Searches through the gateway's own backends are counted and never priced.";
    vi.stubGlobal("fetch", respondWith(report({ disclosures: [note, second] })));
    renderScreen();

    const toggle = await screen.findByRole("button", { name: /2 notes about these figures/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // One line, not two paragraphs: the accessibility tree sees the summary
    // alone until it opens.
    expect(screen.getAllByRole("note")).toHaveLength(1);
    const user = userEvent.setup();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const notes = screen.getAllByRole("note");
    expect(notes.map((el) => el.textContent)).toContain(note);
    expect(notes.map((el) => el.textContent)).toContain(second);
  });

  it("remembers this page's open-or-closed choice", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith(report({ disclosures: ["A first note.", "A second note."] })),
    );
    const first = renderScreen();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /2 notes/ }));
    expect(window.localStorage.getItem("llmp.console.disclosures.reports")).toBe("true");
    first.unmount();

    renderScreen();
    expect(await screen.findByRole("button", { name: /2 notes/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("shows the person's own spend and breakdown", async () => {
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen();

    await waitFor(() => expect(screen.getAllByText("€3.25").length).toBeGreaterThan(0));
    expect(screen.getByText("gpt-4o")).toBeInTheDocument();
  });

  it("reads its own usage, never everyone's", async () => {
    // The one thing that must not drift: this screen is /api/me, and pointing
    // it at the admin report would show a person the whole deployment.
    const fetchMock = respondWith(report());
    vi.stubGlobal("fetch", fetchMock);
    renderScreen();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.every((url) => url.includes("/api/me/"))).toBe(true);
  });

  it("offers no breakdown by user or group", async () => {
    // Every row would be the reader. A control whose every answer is your own
    // name looks broken.
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen();

    const breakdown = await screen.findByLabelText("Breakdown");
    const options = Array.from(breakdown.querySelectorAll("option")).map((o) => o.value);
    expect(options).not.toContain("user");
    expect(options).not.toContain("group");
    expect(options).toContain("model");
  });

  it("says plainly when there is no usage", async () => {
    const empty = report({
      rows: [],
      totals: { ...report().totals, requests: 0, total_tokens: 0, cost: "0" },
    });
    vi.stubGlobal("fetch", respondWith(empty));
    renderScreen();

    // A totals row of zeroes rather than an empty state: the table shows its
    // footer whenever there is one, which is the admin report's behaviour too.
    // "You spent nothing" said as €0.00 in a total is clearer than a table that
    // vanishes.
    await waitFor(() => expect(screen.getAllByText("€0.00").length).toBeGreaterThan(0));
    expect(screen.getByText("Total")).toBeInTheDocument();
  });

  it("can change the breakdown", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = respondWith(report());
    vi.stubGlobal("fetch", fetchMock);
    renderScreen();

    await screen.findByLabelText("Breakdown");
    await user.selectOptions(screen.getByLabelText("Breakdown"), "day");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).includes("group_by=day")),
      ).toBe(true),
    );
  });
});
