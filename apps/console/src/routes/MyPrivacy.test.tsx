import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { MyPrivacy, MyPrivacyTest } from "./MyPrivacy";

/** A user's own pipelines and policies, under what the administrator already set (read only). */

const rule = (id: string, name: string, mode: string) => ({
  id,
  name,
  enabled: true,
  policies: ["pol1"],
  match: "all",
  conditions: [],
  mode,
  overrides: {},
  note: null,
  action: null,
  issues: [],
});

function pipeline(id: string, name: string, owner: string | null, mode: string) {
  return {
    id,
    owner_id: owner,
    kind: "content",
    name,
    enabled: true,
    position: 0,
    who: owner ? { mode: "users", ids: [owner] } : { mode: "all", ids: [] },
    target: { mode: "all", providers: [], models: [], model_types: [] },
    rules: [rule(`r-${id}`, "Names found", mode)],
    labels: {},
    summary: [],
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
  };
}

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function render_(
  element: React.ReactElement,
  seen: Seen[] = [],
  options: Record<string, unknown> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.includes("/in-force"))
        return jsonResponse({ items: [pipeline("a1", "Remote embeddings", null, "block")], total: 1, limit: 200, offset: 0 });
      if (url.includes("/api/me/privacy/pipelines?"))
        return jsonResponse({ items: [pipeline("m1", "My names rule", "me", "block")], total: 1, limit: 200, offset: 0 });
      if (url.includes("/policies") && method === "GET")
        return jsonResponse({
          items: [
            {
              id: "pol1",
              name: "My names",
              entries: [{ kind: "mode", name: "PERSON", regex: null }],
              used_by: [],
              created_at: "",
              updated_at: "",
            },
          ],
          total: 1,
          limit: 200,
          offset: 0,
        });
      if (url.endsWith("/options"))
        return jsonResponse({
          users: [],
          groups: [],
          keys: [],
          providers: [],
          models: [],
          readers: [],
          policies: [],
          own_policies: [],
          classifiers: [],
          entity_types: ["PERSON", "IBAN_CODE"],
          ner_available: true,
          ner_note: null,
          ...options,
        });
      return jsonResponse({});
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("My privacy pipelines", () => {
  it("lists mine, then the administrator's pipelines read only", async () => {
    render_(<MyPrivacy />);
    expect(await screen.findByText("My names rule")).toBeInTheDocument();
    const admin = screen.getByRole("region", { name: "Set by your administrator" });
    expect(within(admin).getByText("Remote embeddings")).toBeInTheDocument();
    expect(within(admin).getByText("Read only")).toBeInTheDocument();
    // Read only: nothing here edits an administrator's pipeline.
    expect(within(admin).queryByRole("button", { name: /edit|delete|turn/i })).toBeNull();
    expect(within(admin).queryByRole("link", { name: "Edit" })).toBeNull();
  });

  it("shows mine as only for me, with edit and delete", async () => {
    render_(<MyPrivacy />);
    await screen.findByText("My names rule");
    expect(screen.getAllByText("Only me").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute("href", "/privacy/pipelines/m1");
  });

  it("edits a policy of their own with the types the administrator offers", async () => {
    const seen: Seen[] = [];
    render_(<MyPrivacy />, seen);
    const user = userEvent.setup();
    await screen.findByText("My names");
    await user.click(within(screen.getByRole("region", { name: "My policies" })).getByRole("button", { name: "New policy" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "Mine");
    await user.click(within(dialog).getByRole("button", { name: "Add an entry" }));
    const types = within(dialog).getByLabelText("Kind of value") as HTMLSelectElement;
    expect([...types.options].map((o) => o.value)).toEqual(["", "PERSON", "IBAN_CODE"]);
    await user.selectOptions(types, "IBAN_CODE");
    // A policy only says what to look for: what happens to it is the pipeline rule's.
    expect(within(dialog).queryByLabelText("What happens to it")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(seen.some((c) => c.method === "POST" && c.url.endsWith("/policies"))).toBe(true));
    expect(seen.find((c) => c.method === "POST" && c.url.endsWith("/policies"))?.body).toEqual({
      name: "Mine",
      entries: [{ kind: "mode", name: "IBAN_CODE", regex: null }],
    });
  });

  it("says no NER engine is installed, and offers no model types, on a pattern-only service", async () => {
    render_(<MyPrivacy />, [], {
      entity_types: ["IBAN_CODE"],
      ner_available: false,
      ner_note: "No NER engine is installed in the redaction service; entities need one.",
    });
    const user = userEvent.setup();
    await screen.findByText("My names");
    await user.click(within(screen.getByRole("region", { name: "My policies" })).getByRole("button", { name: "New policy" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/No NER engine is installed in the redaction service/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "How to enable it" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Add an entry" }));
    const types = within(dialog).getByLabelText("Kind of value") as HTMLSelectElement;
    expect([...types.options].map((o) => o.value)).toEqual(["", "IBAN_CODE"]);
  });

  it("offers templates limited to the types the administrator allows", async () => {
    render_(<MyPrivacy />, [], { entity_types: ["IBAN_CODE"], ner_available: false });
    const user = userEvent.setup();
    await screen.findByText("My names");
    await user.click(screen.getByRole("button", { name: "New policy from template" }));
    const picker = await screen.findByRole("dialog");
    expect(within(picker).getByText(/Skipped: PERSON, LOCATION, which need an NER engine/)).toBeInTheDocument();
    expect(within(picker).getAllByText(/which your administrator has not made available/).length).toBeGreaterThan(0);
    await user.click(within(picker).getByRole("button", { name: "Use the template Personal and banking data" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Name")).toHaveValue("Personal and banking data");
    const types = within(dialog).getAllByLabelText("Kind of value") as HTMLSelectElement[];
    expect(types.map((t) => t.value)).toEqual(["IBAN_CODE"]);
  });

  it("has no detector-profile picker in a user's policy", async () => {
    render_(<MyPrivacy />);
    const user = userEvent.setup();
    await screen.findByText("My names");
    await user.click(within(screen.getByRole("region", { name: "My policies" })).getByRole("button", { name: "New policy" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Detector profile")).toBeNull();
  });

  it("has a test box locked to the user", async () => {
    render_(<MyPrivacyTest />);
    expect(await screen.findByText("Only me")).toBeInTheDocument();
    expect(screen.queryByLabelText("User")).toBeNull();
  });
});
