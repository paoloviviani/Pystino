import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, UsageReport } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { Overview } from "./Overview";

/**
 * What this screen must get right is not layout, it is honesty about money:
 * amounts come from the API as strings and are never recomputed here, and the
 * API's caveats are shown rather than quietly dropped.
 */

const ME: Me = {
  id: "u1",
  email: "alice@example.org",
  display_name: "Alice",
  is_admin: false,
  groups: [{ id: "g1", name: "research", description: null }],
  default_billing_group: { id: "g1", name: "research", description: null },
};

function report(overrides: Partial<UsageReport> = {}): UsageReport {
  return {
    period: {
      label: "2026-08",
      kind: "month",
      start: "2026-07-31T22:00:00Z",
      end: "2026-08-31T22:00:00Z",
      timezone: "Europe/Rome",
    },
    group_by: "model",
    currency: "EUR",
    rows: [
      {
        key: "gpt-ish",
        label: "gpt-ish",
        requests: 12,
        prompt_tokens: 1000,
        completion_tokens: 500,
        total_tokens: 1500,
        images: 0,
        cost: "3.250000000000",
        estimated_requests: 0,
        unavailable_requests: 0,
      },
    ],
    totals: {
      key: null,
      label: "total",
      requests: 12,
      prompt_tokens: 1000,
      completion_tokens: 500,
      total_tokens: 1500,
      images: 0,
      cost: "3.250000000000",
      estimated_requests: 0,
      unavailable_requests: 0,
    },
    disclosures: [],
    ...overrides,
  };
}

function respondWith(body: UsageReport, keys: unknown[] = []) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/reports/usage") ? body : keys;
    return jsonResponse(payload);
  });
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // Pinned so the period picker's default is deterministic; without it the
  // suite would exercise a different month every month.
  vi.setSystemTime(new Date(2026, 7, 15));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Overview", () => {
  it("shows the total the API reported, formatted but not recomputed", async () => {
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getAllByText("€3.25").length).toBeGreaterThan(0));
  });

  it("names the period and its timezone", async () => {
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getByText(/Spend · 2026-08/)).toBeInTheDocument());
    expect(screen.getByText(/Europe\/Rome/)).toBeInTheDocument();
  });

  it("renders the API's disclosures verbatim", async () => {
    // Wording a caveat twice — once in the API, once here — is how the two end
    // up disagreeing about what the number means.
    const note = "3 of 12 requests have estimated token counts: the provider did not report usage.";
    vi.stubGlobal("fetch", respondWith(report({ disclosures: [note] })));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getByText(note)).toBeInTheDocument());
  });

  it("asks for the current month by default", async () => {
    const fetchMock = respondWith(report());
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("period=2026-08"))).toBe(true);
  });

  it("says so plainly when there is no usage", async () => {
    const empty = report({
      rows: [],
      totals: { ...report().totals, requests: 0, total_tokens: 0, cost: "0" },
    });
    vi.stubGlobal("fetch", respondWith(empty));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getAllByText("€0.00").length).toBeGreaterThan(0));
  });

  it("shows the error the API gave rather than a generic one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { message: "'nope' is not a period." } }), {
          status: 400,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    renderScreen(<Overview me={ME} />);

    await waitFor(() =>
      expect(screen.getByText("'nope' is not a period.")).toBeInTheDocument(),
    );
  });
});
