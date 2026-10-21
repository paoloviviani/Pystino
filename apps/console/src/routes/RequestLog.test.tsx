import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RequestLogRow } from "../lib/requestLog";
import { jsonResponse } from "../test-helpers";
import { AdminRequests } from "./AdminRequests";
import { MyRequests } from "./MyRequests";

/** The request log: what it asks the API for, per audience, and that the export
 *  is the filtered set on screen. Content never reaches the screen because the
 *  API never sends it; the row type has no field for it. */

function row(overrides: Partial<RequestLogRow> = {}): RequestLogRow {
  return {
    request_id: "r-1",
    created_at: "2026-08-14T10:00:00Z",
    user_id: "u1",
    user_label: "ada@example.org",
    group_id: "g1",
    group_name: "research",
    requested_model: "fast",
    model: "gpt-real",
    upstream_model: null,
    streamed: false,
    prompt_tokens: 1200,
    completion_tokens: 30,
    cached_prompt_tokens: 7,
    cost: "0.012000000000",
    currency: "EUR",
    status: "completed",
    latency_ms: 842,
    source: "api_key",
    ...overrides,
  };
}

function routes(seen: string[], items: RequestLogRow[] = [row()]) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    if (url.includes("/requests")) {
      return jsonResponse({ items, total: items.length, limit: 50, offset: 0 });
    }
    return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
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
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("request log", () => {
  it("shows the alias beside the model that served it, the source, and the status", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminRequests />);

    await waitFor(() => expect(screen.getByText("ada@example.org")).toBeInTheDocument());
    expect(screen.getByText("fast")).toBeInTheDocument();
    expect(screen.getByText(/gpt-real/)).toBeInTheDocument();
    expect(screen.getByText("API key")).toBeInTheDocument();
    // Scoped to the table: "Completed" is also an option in the status filter.
    expect(within(screen.getByRole("table")).getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("1,200 / 30 / 7")).toBeInTheDocument();
  });

  it("asks the administrator endpoint, and sends a person filter", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(seen));
    renderScreen(<AdminRequests />);
    await waitFor(() => expect(screen.getByText("ada@example.org")).toBeInTheDocument());
    expect(seen.some((url) => url.startsWith("/api/admin/requests?"))).toBe(true);
    expect(screen.getByLabelText("Person")).toBeInTheDocument();
  });

  it("the personal view uses the personal endpoint and offers no person filter", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(seen));
    renderScreen(<MyRequests />);
    await waitFor(() => expect(screen.getByText("fast")).toBeInTheDocument());
    expect(seen.some((url) => url.startsWith("/api/me/requests?"))).toBe(true);
    expect(seen.some((url) => url.includes("/api/admin/requests"))).toBe(false);
    expect(screen.queryByLabelText("Person")).not.toBeInTheDocument();
    expect(seen.every((url) => !url.includes("user_id"))).toBe(true);
  });

  it("a status filter reaches the API and resets to the first page", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", routes(seen));
    renderScreen(<AdminRequests />);
    await waitFor(() => expect(screen.getByText("ada@example.org")).toBeInTheDocument());
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.selectOptions(screen.getByLabelText("Status"), "upstream_error");
    await waitFor(() =>
      expect(seen.some((url) => url.includes("status=upstream_error") && url.includes("offset=0"))).toBe(true),
    );
  });

  it("says plainly that no content is stored", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<MyRequests />);
    await waitFor(() =>
      expect(screen.getByText("No prompt or reply text is stored in this log.")).toBeInTheDocument(),
    );
  });

  it("exports the same filtered set", async () => {
    vi.stubGlobal("fetch", routes([]));
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    renderScreen(<AdminRequests />);
    await waitFor(() => expect(screen.getByText("ada@example.org")).toBeInTheDocument());
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.selectOptions(screen.getByLabelText("Status"), "blocked");
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(assign).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/admin\/requests\.csv\?.*status=blocked/),
    );
  });
});
