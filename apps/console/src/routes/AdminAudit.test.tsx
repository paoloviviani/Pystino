import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AuditEvent } from "../lib/adminAudit";
import { jsonResponse } from "../test-helpers";
import { AdminAudit } from "./AdminAudit";

/** The audit log screen: what it asks for, and how a change is shown. */

function event(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: "e1",
    created_at: "2026-10-20T10:00:00Z",
    actor_user_id: "u1",
    actor_label: "root@example.org",
    action: "provider.update",
    target_type: "provider",
    target_id: "p1",
    target_label: "acme",
    changes: {
      description: { before: "old text", after: "new text" },
      api_key: { after: "rotated" },
    },
    ...overrides,
  };
}

function mockApi(seen: string[], items: AuditEvent[]) {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    seen.push(String(input));
    return jsonResponse({ items, total: items.length, limit: 50, offset: 0 });
  });
  vi.stubGlobal("fetch", fetcher);
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminAudit />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("audit log", () => {
  it("lists who, what and the before and after of each change", async () => {
    const seen: string[] = [];
    mockApi(seen, [event()]);
    renderScreen();

    expect(await screen.findByText("root@example.org")).toBeInTheDocument();
    expect(screen.getByText("provider.update")).toBeInTheDocument();
    expect(screen.getByText("acme")).toBeInTheDocument();
    expect(seen[0]).toContain("/api/admin/audit?");

    await userEvent.click(screen.getByText("Changes (2)"));
    expect(screen.getByText("description")).toBeInTheDocument();
    expect(screen.getByText(/was old text/)).toBeInTheDocument();
    expect(screen.getByText(/now new text/)).toBeInTheDocument();
    expect(screen.getByText(/now rotated/)).toBeInTheDocument();
  });

  it("sends the chosen filters and starts again from the first page", async () => {
    const seen: string[] = [];
    mockApi(seen, [event()]);
    renderScreen();
    await screen.findByText("root@example.org");

    await userEvent.selectOptions(screen.getByLabelText("Action"), "grant.");
    await userEvent.selectOptions(screen.getByLabelText("Target"), "grant");
    await waitFor(() => {
      const last = new URL(seen[seen.length - 1] ?? "", "http://x");
      expect(last.searchParams.get("action")).toBe("grant.");
      expect(last.searchParams.get("target_type")).toBe("grant");
      expect(last.searchParams.get("offset")).toBe("0");
    });

    await userEvent.type(screen.getByLabelText("From"), "2026-10-01");
    await waitFor(() => {
      const last = new URL(seen[seen.length - 1] ?? "", "http://x");
      expect(last.searchParams.get("start")).not.toBeNull();
    });
  });

  it("says so when nothing was recorded", async () => {
    mockApi([], []);
    renderScreen();
    expect(await screen.findByText("No changes recorded for these filters.")).toBeInTheDocument();
  });
});
