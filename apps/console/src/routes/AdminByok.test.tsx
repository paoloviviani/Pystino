import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ByokModel, ByokProvider } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminByok } from "./AdminByok";

/**
 * Private providers, for administrators: what users added with their own keys.
 *
 * The property worth pinning is what the page never has: a credential. The API
 * does not return one, and the screen must not grow a field, a hint or a
 * placeholder suggesting one exists to be shown.
 */

const OWNER = { id: "u1", email: "ada@example.org", name: "Ada Lovelace" };
const USAGE = { requests: 7, total_tokens: 1234, cost: "0.5", last_used_at: "2026-08-14T10:00:00Z" };

function provider(overrides: Partial<ByokProvider> = {}): ByokProvider {
  return {
    id: "pr1",
    name: "ada-vllm",
    kind: "provider",
    plugin: null,
    base_url_host: "llm.example.org",
    has_api_key: true,
    is_active: true,
    admin_disabled: false,
    model_count: 2,
    owner: OWNER,
    usage: USAGE,
    created_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

function model(overrides: Partial<ByokModel> = {}): ByokModel {
  return {
    id: "m1",
    name: "llama",
    api_name: "my/llama",
    kind: "chat",
    upstream_model: "meta/llama-3",
    provider_id: "pr1",
    provider_name: "ada-vllm",
    is_active: true,
    admin_disabled: false,
    owner: OWNER,
    usage: USAGE,
    created_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

function gateway(calls: { url: string; method: string }[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (method === "POST") return jsonResponse(provider({ admin_disabled: true }));
    if (url.includes("/api/admin/byok/providers")) return jsonResponse([provider()]);
    if (url.includes("/api/admin/byok/models")) return jsonResponse([model()]);
    return jsonResponse([]);
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminByok />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminByok", () => {
  it("lists owner, provider, host, models and usage, and no key of any kind", async () => {
    vi.stubGlobal("fetch", gateway([]));
    renderPage();

    expect((await screen.findAllByText("ada-vllm")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Ada Lovelace").length).toBeGreaterThan(0);
    expect(screen.getAllByText("ada@example.org").length).toBeGreaterThan(0);
    // The host, never the full URL, whose path can carry a token.
    expect(screen.getByText("llm.example.org")).toBeInTheDocument();
    expect(screen.getAllByText("7 requests").length).toBeGreaterThan(0);
    expect(screen.getByText("my/llama")).toBeInTheDocument();
    // Nothing on the page speaks of a credential: not a key column, not a hint,
    // not a "no key" placeholder.
    expect(screen.queryByText(/api key/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/hint/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/sk-/)).not.toBeInTheDocument();
  });

  it("holds a provider after a confirmation, through the disable route", async () => {
    const user = userEvent.setup({ delay: null });
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal("fetch", gateway(calls));
    renderPage();

    await screen.findAllByText("ada-vllm");
    const providers = within(screen.getAllByRole("table")[0]!);
    await user.click(providers.getByRole("button", { name: "Disable" }));

    // Asked first: nothing has been sent yet.
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/the owner cannot undo this/i)).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);

    await user.click(dialog.getByRole("button", { name: "Disable" }));
    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.method === "POST" && call.url === "/api/admin/byok/providers/pr1/disable",
        ),
      ).toBe(true),
    );
  });

  it("shows a held row as held and offers Enable", async () => {
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        calls.push({ url, method: "GET" });
        if (url.includes("/byok/providers")) return jsonResponse([provider({ admin_disabled: true })]);
        return jsonResponse([]);
      }),
    );
    renderPage();

    expect(await screen.findByText("Held by an administrator")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeInTheDocument();
  });
});
