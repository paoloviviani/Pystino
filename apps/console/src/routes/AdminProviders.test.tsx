import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminProvider } from "../lib/types";
import { AdminProviders } from "./AdminProviders";

/**
 * The provider screen handles credentials, so the properties worth pinning are
 * about what it does *not* do: never show a key, never send one the operator
 * did not type, and never wipe a stored key as a side effect of editing
 * something else.
 */

function provider(overrides: Partial<AdminProvider> = {}): AdminProvider {
  return {
    id: "pr1",
    name: "acme",
    description: "Commercial API",
    base_url: "https://acme.test/v1",
    api_key_hint: "sk-a…3456",
    has_api_key: true,
    extra_headers: {},
    is_active: true,
    forward_stream_options: true,
    model_count: 3,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

interface Captured {
  bodies: { url: string; body: unknown }[];
}

function routes(providers: AdminProvider[], captured: Captured = { bodies: [] }, test?: unknown) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (init?.body) captured.bodies.push({ url, body: JSON.parse(String(init.body)) });

    let payload: unknown = [];
    if (url.endsWith("/test") && method === "POST") {
      payload = test ?? { ok: true, status_code: 200, detail: "reachable", model_count: 4, sample: [], latency_ms: 42 };
    } else if (url.includes("/api/admin/providers")) {
      payload = method === "GET" ? providers : providers[0];
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
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

afterEach(() => vi.unstubAllGlobals());

describe("AdminProviders", () => {
  it("lists a provider with its endpoint", async () => {
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    expect(screen.getByText("https://acme.test/v1")).toBeInTheDocument();
  });

  it("shows only a hint of the key, never the key", async () => {
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("sk-a…3456")).toBeInTheDocument());
  });

  it("says when a provider has no credential", async () => {
    // A local vLLM or Ollama usually needs none, and "none" is different from
    // "we lost it".
    vi.stubGlobal("fetch", routes([provider({ has_api_key: false, api_key_hint: "" })]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("none")).toBeInTheDocument());
  });

  it("shows how many models a provider serves", async () => {
    // The blast radius of deactivating or deleting it.
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
  });

  it("reports a successful test with what the provider offers", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([provider()]));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(screen.getByText(/4 models/)).toBeInTheDocument());
  });

  it("shows why a test failed rather than a generic error", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routes([provider()], { bodies: [] }, {
        ok: false,
        status_code: 401,
        detail: "the provider answered 401 — check the API key",
        model_count: null,
        sample: [],
        latency_ms: 88,
      }),
    );
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() =>
      expect(
        screen.getByText("the provider answered 401 — check the API key"),
      ).toBeInTheDocument(),
    );
  });

  it("does not send an api_key when the field was left alone", async () => {
    // The three-way convention. Editing a base URL must not wipe the stored
    // credential, and sending "" would do exactly that.
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    const url = within(dialog).getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "https://acme.test/v2");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    const body = captured.bodies[0]?.body as Record<string, unknown>;
    expect(body.base_url).toBe("https://acme.test/v2");
    expect("api_key" in body).toBe(false);
  });

  it("sends an empty api_key only when removal is chosen explicitly", async () => {
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Remove the stored key"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0]?.body as Record<string, unknown>).api_key).toBe("");
  });

  it("sends a typed key as the replacement", async () => {
    const user = userEvent.setup();
    const captured: Captured = { bodies: [] };
    vi.stubGlobal("fetch", routes([provider()], captured));
    renderScreen(<AdminProviders />);

    await waitFor(() => expect(screen.getByText("acme")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("API key"), "sk-new-999999");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(captured.bodies.length).toBeGreaterThan(0));
    expect((captured.bodies[0]?.body as Record<string, unknown>).api_key).toBe("sk-new-999999");
  });

  it("says plainly when nothing is configured", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminProviders />);

    await waitFor(() =>
      expect(
        screen.getByText("No providers configured. Nothing can be served until one exists."),
      ).toBeInTheDocument(),
    );
  });

  it("will not create a provider without a name and endpoint", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routes([]));
    renderScreen(<AdminProviders />);

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeDisabled();

    await user.type(within(dialog).getByLabelText("Name"), "ollama");
    await user.type(within(dialog).getByLabelText("Base URL"), "http://ollama:11434/v1");
    expect(within(dialog).getByRole("button", { name: "Add" })).toBeEnabled();
  });
});
