import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

/** Old Redaction and Privacy routing links keep working; Privacy is one area with four tabs (Pipelines, Redaction policies, Detectors, Test). */

function Where() {
  const loc = useLocation();
  return <p data-testid="where">{loc.pathname}</p>;
}

const ME = {
  id: "u1",
  email: "admin@example.org",
  display_name: "Admin",
  is_admin: true,
  groups: [],
  default_billing_group: null,
  issuer: "local",
  has_password: true,
  capabilities: { code_agents: true, api_keys: true, local_connectors: true, byok: false, console: true },
  chat_enabled: true,
  chat_url: "https://chat.example.org",
};

/** Just enough of the redaction status for the tabs to draw. */
const STATUS = {
  engine: "noop",
  enabled: false,
  installed_engines: ["noop"],
  engines: [],
  warnings: [],
  service: null,
  activity: { window_seconds: 86400, requests: 0, requests_redacted: 0, entities_redacted: 0, engines: [] },
};

function go(path: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith("/api/me")
        ? ME
        : url.endsWith("/api/admin/redaction")
          ? STATUS
          : { items: [], total: 0, limit: 50, offset: 0 };
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Privacy routes", () => {
  it.each([
    // A redaction rule is now a policy under the same id, so an old link lands on the same thing.
    ["/admin/redaction", "/admin/privacy/policies"],
    ["/admin/redaction/rules/abc", "/admin/privacy/policies/abc"],
    ["/admin/redaction/rules/new", "/admin/privacy/policies/new"],
    ["/admin/privacy/hidden", "/admin/privacy/policies"],
    ["/admin/privacy/hidden/rules/abc", "/admin/privacy/policies/abc"],
    ["/admin/privacy/rules/abc", "/admin/privacy"],
    ["/admin/privacy/profiles", "/admin/privacy/detectors"],
    ["/admin/privacy/anything-else", "/admin/privacy"],
  ])("redirects %s to %s", async (from, to) => {
    go(from);
    await vi.waitFor(() => expect(screen.getByTestId("where").textContent).toBe(to));
  });
});
