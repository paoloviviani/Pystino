import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPolicies } from "./AdminPolicies";

/** The Redaction policies tab: what to hide, with no scope; a policy a pipeline applies cannot be deleted. */

const POLICY = (over: Record<string, unknown>) => ({
  id: "p1",
  name: "Default",
  policy: {
    default_mode: "off",
    entities: { PERSON: { mode: "anonymise_restore", threshold: null, placeholder: "tagged" } },
    patterns: [],
    allow_list: [],
  },
  profile_id: null,
  profile_name: "Fast",
  is_active: true,
  reason: "",
  used_by: [],
  created_by: null,
  created_by_email: null,
  created_at: "2026-11-08T10:00:00Z",
  updated_at: "2026-11-08T10:00:00Z",
  ...over,
});

function renderPage(items: unknown[], seen: string[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      seen.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/policies") && (init?.method ?? "GET") === "GET") {
        return jsonResponse({ items, total: items.length, limit: 200, offset: 0 });
      }
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return jsonResponse({});
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPolicies />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Redaction policies", () => {
  it("lists policies with what they hide, their profile and the pipelines that apply them", async () => {
    renderPage([POLICY({ used_by: ["Remote embeddings", "Chat"] })]);
    expect(await screen.findByText("Default")).toBeInTheDocument();
    expect(screen.getByText("Fast")).toBeInTheDocument();
    expect(screen.getByText("used by 2")).toBeInTheDocument();
    expect(screen.getByText("Used by: Remote embeddings, Chat")).toBeInTheDocument();
    // No scope anywhere: nothing says user, group, provider or model.
    expect(screen.queryByText(/group|provider|model/i)).toBeNull();
  });

  it("will not delete a policy that a pipeline applies, and says which", async () => {
    const seen: string[] = [];
    renderPage([POLICY({ used_by: ["Chat"] })], seen);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/In use, cannot delete: Chat/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(seen.some((s) => s.startsWith("DELETE"))).toBe(false);
  });

  it("deletes an unused policy after asking", async () => {
    const seen: string[] = [];
    renderPage([POLICY({})], seen);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(seen.some((s) => s.startsWith("DELETE") && s.endsWith("/policies/p1"))).toBe(true));
  });
});
