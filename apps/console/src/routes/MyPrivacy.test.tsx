import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { MyPrivacy, MyPrivacyTest } from "./MyPrivacy";

/** A user's own pipelines and policies, under what the administrator already set (read only). */

const rule = (id: string, name: string, level: string, key = "health") => ({
  id,
  name,
  enabled: true,
  observe_only: false,
  conditions: [{ key, values: [], negate: false, decide: "patterns" }],
  action: { level, reader: null, engine: null, redact_with: null },
  issues: [],
});

function pipeline(id: string, name: string, owner: string | null, level: string) {
  return {
    id,
    owner_id: owner,
    kind: "embed",
    name,
    enabled: true,
    position: 0,
    who: owner ? { mode: "users", ids: [owner] } : { mode: "all", ids: [] },
    target: { mode: "all", providers: [], models: [] },
    rules: [rule(`r-${id}`, "Health", level)],
    otherwise: { ...rule(`o-${id}`, "Everything else", "pass"), conditions: [] },
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

function render_(element: React.ReactElement, seen: Seen[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.includes("/in-force"))
        return jsonResponse({ items: [pipeline("a1", "Remote embeddings", null, "refuse")], total: 1, limit: 200, offset: 0 });
      if (url.includes("/api/me/privacy/pipelines?"))
        return jsonResponse({ items: [pipeline("m1", "My health rule", "me", "refuse")], total: 1, limit: 200, offset: 0 });
      if (url.includes("/policies") && method === "GET")
        return jsonResponse({
          items: [
            {
              id: "pol1",
              name: "My names",
              entries: [{ kind: "mode", name: "PERSON", regex: null, mode: "redact" }],
              profile_id: null,
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
          profiles: [],
          entity_types: ["PERSON", "IBAN_CODE"],
          engines: [],
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
    expect(await screen.findByText("My health rule")).toBeInTheDocument();
    const admin = screen.getByRole("region", { name: "Set by your administrator" });
    expect(within(admin).getByText("Remote embeddings")).toBeInTheDocument();
    expect(within(admin).getByText("Read only")).toBeInTheDocument();
    // Read only: nothing here edits an administrator's pipeline.
    expect(within(admin).queryByRole("button", { name: /edit|delete|turn/i })).toBeNull();
    expect(within(admin).queryByRole("link", { name: "Edit" })).toBeNull();
  });

  it("shows mine as only for me, with edit and delete", async () => {
    render_(<MyPrivacy />);
    await screen.findByText("My health rule");
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
    // There is no "off" to pick: a rule of theirs can only add protection.
    const modes = within(dialog).getByLabelText("What happens to it") as HTMLSelectElement;
    expect([...modes.options].map((o) => o.value)).toEqual(["anonymise_restore", "anonymise", "redact", "block"]);
    await user.click(within(dialog).getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(seen.some((c) => c.method === "POST" && c.url.endsWith("/policies"))).toBe(true));
    expect(seen.find((c) => c.method === "POST" && c.url.endsWith("/policies"))?.body).toEqual({
      name: "Mine",
      profile_id: null,
      entries: [{ kind: "mode", name: "IBAN_CODE", mode: "redact", regex: null }],
    });
  });

  it("has a test box locked to the user", async () => {
    render_(<MyPrivacyTest />);
    expect(await screen.findByText("Only me")).toBeInTheDocument();
    expect(screen.queryByLabelText("User")).toBeNull();
  });
});
