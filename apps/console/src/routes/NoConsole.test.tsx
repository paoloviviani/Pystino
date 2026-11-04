import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { jsonResponse } from "../test-helpers";
import { NoConsole } from "./NoConsole";

const ME = {
  id: "u1",
  email: "alice@example.org",
  display_name: "Alice",
  is_admin: false,
  groups: [],
  default_billing_group: null,
  issuer: "local",
  has_password: true,
  capabilities: { code_agents: true, api_keys: true, local_connectors: true, byok: false, console: false },
  chat_enabled: true,
  chat_url: "https://chat.example.org",
};

function renderApp(me: unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith("/api/me")) return jsonResponse(me);
    return jsonResponse({ items: [], total: 0, limit: 50, offset: 0 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("NoConsole", () => {
  it("links back to the chat only when the deployment said where it is", () => {
    const { rerender } = render(<NoConsole chatUrl="https://chat.example.org" />);
    expect(screen.getByRole("link", { name: "Open the chat" }).getAttribute("href")).toBe(
      "https://chat.example.org",
    );
    rerender(<NoConsole chatUrl="" />);
    expect(screen.queryByRole("link")).toBeNull();
    rerender(<NoConsole chatUrl="javascript:alert(1)" />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("App without console access", () => {
  it("lands a signed-in member on the plain page, with no navigation and no console calls", async () => {
    const fetchMock = renderApp(ME);
    expect(await screen.findByText("The console is not available to you")).toBeInTheDocument();
    expect(document.body.textContent).toContain("managed through the chat");
    expect(screen.queryByRole("navigation")).toBeNull();
    const paths = fetchMock.mock.calls.map(([url]) => String(url));
    expect(paths.filter((p) => p.includes("/api/me/") || p.includes("/api/admin"))).toEqual([]);
  });

  it("does not apply to an administrator", async () => {
    renderApp({ ...ME, is_admin: true });
    expect(await screen.findByRole("navigation")).toBeInTheDocument();
    expect(screen.queryByText("The console is not available to you")).toBeNull();
  });

  it("treats an absent flag as allowed (an older gateway)", async () => {
    const { console: _omit, ...caps } = ME.capabilities;
    renderApp({ ...ME, capabilities: caps });
    expect(await screen.findByRole("navigation")).toBeInTheDocument();
  });
});
