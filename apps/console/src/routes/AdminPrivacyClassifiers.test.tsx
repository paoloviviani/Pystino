import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacyClassifiers } from "./AdminPrivacyClassifiers";

/** The Classifiers tab: decision models with a topic; classifying sends the text to the model's provider. */

const MODELS = [{ id: "m1", name: "chat-large", provider: "Acme AI", local: false }];
const LOCAL_MODELS = [{ id: "l1", name: "inside-chat", provider: "Built-in", local: true }];

const CLASSIFIER = (over: Record<string, unknown> = {}) => ({
  id: "c1",
  name: "Unannounced project",
  topic: "Texts about a project that has not been announced",
  model_id: "m1",
  model_name: "chat-large",
  provider_name: "Acme AI",
  available: true,
  why_not: "",
  rules_using: 2,
  created_at: "2026-11-08T10:00:00Z",
  updated_at: "2026-11-08T10:00:00Z",
  ...over,
});

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function renderPage(
  items: unknown[],
  models: unknown[],
  seen: Seen[] = [],
  deleteFails = false,
  allowRemote = true,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/privacy/settings")) {
        if (method === "PUT") return jsonResponse(JSON.parse(String(init?.body)));
        return jsonResponse({ user_entity_types: null, allow_remote_classifiers: allowRemote });
      }
      if (method === "GET") return jsonResponse({ items, models, allow_remote: allowRemote });
      if (method === "DELETE") {
        return deleteFails
          ? jsonResponse({ error: { message: "Used by the rule Names in Remote embeddings.", code: "in_use" } }, 409)
          : new Response(null, { status: 204 });
      }
      return jsonResponse(CLASSIFIER());
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPrivacyClassifiers />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Classifiers", () => {
  it("says it needs a model provider when there is no chat model, with a link to Providers", async () => {
    renderPage([], []);
    expect(await screen.findByText("Needs a model provider")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Providers" })).toHaveAttribute("href", "/admin/providers");
    expect(screen.getByRole("button", { name: "New classifier" })).toBeDisabled();
  });

  it("with no local model and remote ones not allowed, explains why and points to the setting", async () => {
    renderPage([], [], [], false, false);
    expect(await screen.findByText("No model that runs inside this deployment")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Go to Providers" })).toBeNull();
    expect(screen.getByRole("switch", { name: "Allow remote classifiers" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("button", { name: "New classifier" })).toBeDisabled();
  });

  it("offers only what the server lists, and a local model carries no provider warning", async () => {
    renderPage([], LOCAL_MODELS, [], false, false);
    const user = userEvent.setup({ delay: null });
    await user.click(await screen.findByRole("button", { name: "New classifier" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("option", { name: /Acme AI/ })).toBeNull();
    await user.selectOptions(within(dialog).getByLabelText("Model"), "l1");
    expect(within(dialog).getByText(/text does not leave it/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/sends the text to/)).toBeNull();
  });

  it("switches remote classifiers on with a PUT that keeps the entity types", async () => {
    const seen: Seen[] = [];
    renderPage([], LOCAL_MODELS, seen, false, false);
    const user = userEvent.setup({ delay: null });
    await user.click(await screen.findByRole("switch", { name: "Allow remote classifiers" }));
    await waitFor(() => expect(seen.some((c) => c.method === "PUT")).toBe(true));
    expect(seen.find((c) => c.method === "PUT")).toMatchObject({
      url: "/api/admin/privacy/settings",
      body: { user_entity_types: null, allow_remote_classifiers: true },
    });
  });

  it("lists a classifier with its topic, model, availability and the rules using it", async () => {
    renderPage([CLASSIFIER()], MODELS);
    expect(await screen.findByText("Unannounced project")).toBeInTheDocument();
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText("Used by 2 rules")).toBeInTheDocument();
    expect(screen.getByText("Model: Acme AI · chat-large")).toBeInTheDocument();
    expect(screen.queryByText("Needs a model provider")).toBeNull();
  });

  it("says why a classifier is unavailable", async () => {
    renderPage([CLASSIFIER({ available: false, why_not: "its model is switched off", model_id: null, model_name: null, provider_name: null })], MODELS);
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/its model is switched off/)).toBeInTheDocument();
  });

  it("warns, where the model is chosen, that classifying sends the text to its provider", async () => {
    const seen: Seen[] = [];
    renderPage([], MODELS, seen);
    const user = userEvent.setup({ delay: null });
    await user.click(await screen.findByRole("button", { name: "New classifier" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Classifying sends the text to the model's provider.")).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Model"), "m1");
    expect(within(dialog).getByText(/sends the text to Acme AI/)).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Name"), "Unannounced project");
    await user.type(within(dialog).getByLabelText(/^Topic/), "Texts about a project that has not been announced");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen.some((c) => c.method === "POST")).toBe(true));
    expect(seen.find((c) => c.method === "POST")).toMatchObject({
      url: "/api/admin/privacy/classifiers",
      body: {
        name: "Unannounced project",
        topic: "Texts about a project that has not been announced",
        model_id: "m1",
      },
    });
  });

  it("edits with a PATCH", async () => {
    const seen: Seen[] = [];
    renderPage([CLASSIFIER()], MODELS, seen);
    const user = userEvent.setup({ delay: null });
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "Renamed");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(seen.some((c) => c.method === "PATCH")).toBe(true));
    expect(seen.find((c) => c.method === "PATCH")).toMatchObject({
      url: "/api/admin/privacy/classifiers/c1",
      body: { name: "Renamed", model_id: "m1" },
    });
  });

  it("deletes after asking, and shows the server's refusal when a rule names it", async () => {
    const seen: Seen[] = [];
    renderPage([CLASSIFIER()], MODELS, seen, true);
    const user = userEvent.setup({ delay: null });
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(seen.some((c) => c.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(seen.some((c) => c.method === "DELETE")).toBe(true));
    expect(await screen.findByText("Used by the rule Names in Remote embeddings.")).toBeInTheDocument();
  });

  it("says in a tooltip that it is a decision model for larger deployments", async () => {
    renderPage([], MODELS);
    expect(await screen.findByRole("button", { name: /^For larger deployments/ })).toBeInTheDocument();
  });
});
