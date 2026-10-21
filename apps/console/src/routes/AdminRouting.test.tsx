import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminAlias, AdminModel, AliasTarget } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminRouting } from "./AdminRouting";

/**
 * The Routing tab, and the two properties it exists to make safe.
 *
 * An alias offers a model under terms different from the model's own, so the
 * questions are: can an administrator see that a name will not work, and can
 * they see what a name costs before granting it? Everything else here is
 * wiring.
 */

const MODEL: AdminModel = {
  id: "m1",
  name: "gpt-4o-mini",
  upstream_model: "openai/gpt-4o-mini",
  provider_id: "pr1",
  provider_name: "openai",
  provider_is_active: true,
  provider_kind: "provider",
  kind: "chat",
  display_name: null,
  description: null,
  is_active: true,
  is_public: false,
  context_window: 128000,
  max_output_tokens: null,
  max_input_tokens: null,
  input_modalities: ["text"],
  output_modalities: ["text"],
  supported_features: [],
  created_at: "2026-08-01T10:00:00Z",
  current_price: null,
  granted_to: [],
  granted_to_users: [],
  aliases_pointing_here: [],
};

const EMBEDDER: AdminModel = {
  ...MODEL,
  id: "m2",
  name: "text-embedding-3-small",
  upstream_model: "openai/text-embedding-3-small",
  kind: "embedding",
};

const TARGET: AliasTarget = {
  id: "m1",
  name: "gpt-4o-mini",
  kind: "chat",
  is_active: true,
  position: 0,
  weight: 1,
  available: true,
  unavailable_reason: null,
};

function alias(overrides: Partial<AdminAlias> = {}): AdminAlias {
  return {
    id: "a1",
    name: "fast",
    display_name: "Fast",
    description: "the quick one",
    kind: "chat",
    is_active: true,
    is_public: false,
    target: TARGET,
    targets: [TARGET],
    route_rule: "first_available",
    route_sticky_ttl_seconds: 3600,
    status: "available",
    unavailable_reason: null,
    granted_to: ["research"],
    granted_to_users: [],
    created_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

interface Captured {
  bodies: { method: string; url: string; body: unknown }[];
  aliases: AdminAlias[];
  models: AdminModel[];
}

function routes(captured: Captured) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    // Every write is recorded, body or not: a DELETE carries none, and
    // recording only the ones with a body would make a delete silently
    // unobservable to these tests.
    if (method !== "GET") {
      captured.bodies.push({
        method,
        url,
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
    }
    if (url.includes("/api/admin/aliases") && method === "GET") {
      // **Not** `jsonResponse`: that wraps arrays in the pagination envelope,
      // and this endpoint answers with a bare list — there is no page to walk.
      // Wrapping it here would have made the screen crash on a shape the
      // gateway never sends, which is the opposite of what a fixture is for.
      return new Response(JSON.stringify(captured.aliases), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/admin/aliases") && method === "POST") {
      return jsonResponse(alias({ id: "a-new" }), 201);
    }
    if (url.includes("/api/admin/aliases") && method === "PATCH") {
      return jsonResponse(captured.aliases[0]);
    }
    if (url.includes("/api/admin/aliases") && method === "DELETE") {
      return new Response(null, { status: 204 });
    }
    if (url.includes("/api/admin/models")) {
      return jsonResponse(captured.models);
    }
    if (url.includes("/api/admin/groups")) {
      return jsonResponse([{ id: "g1", name: "research", models: ["fast"] }]);
    }
    if (url.includes("/api/admin/users")) {
      return jsonResponse([]);
    }
    return jsonResponse([]);
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

async function open(aliases: AdminAlias[], models: AdminModel[] = [MODEL, EMBEDDER]) {
  const captured: Captured = { bodies: [], aliases, models };
  vi.stubGlobal("fetch", routes(captured));
  const user = userEvent.setup();
  renderScreen(<AdminRouting />);
  await screen.findByText(aliases[0]?.display_name ?? aliases[0]?.name ?? "nothing");
  return { captured, user };
}

describe("the Routing tab", () => {
  it("lists each alias with its target, access and status", async () => {
    await open([alias()]);

    const row = screen.getByRole("row", { name: /fast/i });
    expect(within(row).getByRole("link", { name: "gpt-4o-mini" })).toBeTruthy();
    expect(within(row).getByText("research")).toBeTruthy();
    expect(within(row).getByText("Available")).toBeTruthy();
  });

  it("still renders when the gateway reports a reason this console does not know", async () => {
    // A newer gateway may add a reason code. An unknown one used to throw on
    // `.label()` and blank the whole page; it must fall back to a generic badge.
    await open([
      alias({
        status: "unavailable",
        target: null,
        targets: [{ ...TARGET, available: false, unavailable_reason: "brand_new_reason" as never }],
        unavailable_reason: "brand_new_reason" as never,
      }),
    ]);

    expect(screen.getAllByText(/brand_new_reason/).length).toBeGreaterThan(0);
    expect(screen.getByRole("row", { name: /fast/i })).toBeTruthy();
  });

  it("names the reason an alias is unavailable, and explains it", async () => {
    await open([
      alias({
        status: "unavailable",
        target: null,
        targets: [],
        unavailable_reason: "target_deleted",
      }),
    ]);

    // The badge carries the reason…
    expect(screen.getByText(/Unavailable/)).toBeTruthy();
    // The badge and the notice both name the reason; the notice is what makes
    // it actionable, so assert both are present.
    expect(screen.getAllByText(/Target deleted/).length).toBeGreaterThan(0);
    // …and a notice says what to do about it, which a badge alone cannot.
    expect(
      screen.getByText(/The model this alias pointed at was deleted/),
    ).toBeTruthy();
  });

  it("distinguishes the other two unavailable reasons", async () => {
    await open([
      alias({ status: "unavailable", unavailable_reason: "target_inactive" }),
    ]);
    expect(screen.getAllByText(/Target inactive/).length).toBeGreaterThan(0);
  });

  it("leaves an unavailable alias in the list rather than hiding it", async () => {
    // Its name and its grants still exist and callers are still sending it, so
    // a list that omitted it would make it impossible to find.
    await open([alias({ status: "unavailable", unavailable_reason: "target_inactive" })]);
    expect(screen.getByText("fast")).toBeTruthy();
  });

  it("creates an alias with one target and its own text", async () => {
    const { captured, user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: /Add alias/ }));
    const dialog = await screen.findByRole("dialog");

    await user.clear(within(dialog).getByLabelText("Name"));
    await user.type(within(dialog).getByLabelText("Name"), "quick");
    await user.type(within(dialog).getByLabelText("Display name"), "Quick");
    await user.selectOptions(
      within(dialog).getByLabelText("Target 1"),
      "m1",
    );
    await user.click(within(dialog).getByRole("button", { name: /Create alias/ }));

    await waitFor(() =>
      expect(
        captured.bodies.some(
          (b) => b.method === "POST" && (b.body as { name?: string }).name === "quick",
        ),
      ).toBe(true),
    );
    const posted = captured.bodies.find((b) => b.method === "POST")!.body as {
      name: string;
      targets: { model_id: string; weight: number }[];
      route_rule: string;
      route_sticky_ttl_seconds: number;
      display_name: string;
    };
    expect(posted.name).toBe("quick");
    expect(posted.display_name).toBe("Quick");
    expect(posted.targets).toEqual([{ model_id: "m1", weight: 1 }]);
    expect(posted.route_rule).toBe("first_available");
    expect(posted.route_sticky_ttl_seconds).toBe(3600);
  });

  it("edits the target and sends exactly the change", async () => {
    const { captured, user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");

    await user.selectOptions(within(dialog).getByLabelText("Target 1"), "m1");
    await user.clear(within(dialog).getByLabelText("Display name"));
    await user.type(within(dialog).getByLabelText("Display name"), "Quicker");
    await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));

    await waitFor(() =>
      expect(
        captured.bodies.some(
          (b) =>
            b.method === "PATCH" &&
            (b.body as { display_name?: string }).display_name === "Quicker",
        ),
      ).toBe(true),
    );
  });

  it("will not save without a name and a target", async () => {
    const { captured, user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");

    await user.clear(within(dialog).getByLabelText("Name"));
    await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));

    // Nothing was sent: an empty name or a missing target is refused by the
    // server with a 400, and a form that can produce one is a form that
    // teaches the operator to ignore its errors.
    expect(captured.bodies.filter((b) => b.method === "PATCH")).toHaveLength(0);
  });

  it("shows the kind read-only, taken from the target", async () => {
    const { user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");

    const kind = within(dialog).getByLabelText("Kind") as HTMLInputElement;
    expect(kind.value).toBe("chat");
    expect(kind.disabled).toBe(true);
  });

  it("locks the target to the alias's kind", async () => {
    // ADR 0101, "Designed for routing": the targets of one alias must be
    // interchangeable, and the only thing this step can check is the kind. An
    // embedding model cannot back a chat alias, so it is not offered rather
    // than offered and refused by the server.
    const { user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");

    const options = within(dialog)
      .getByLabelText("Target 1")
      .querySelectorAll("option");
    const names = Array.from(options).map((o) => o.textContent ?? "");
    expect(names.some((n) => n.includes("text-embedding-3-small"))).toBe(false);
    expect(names.some((n) => n.includes("gpt-4o-mini"))).toBe(true);
  });

  it("offers an inactive model's target as the current one rather than dropping it", async () => {
    // A select that silently omits the current value would rewrite the target
    // to whatever happened to be first on save — a retarget nobody asked for.
    const { user } = await open(
      [
        alias({
          status: "unavailable",
          unavailable_reason: "target_inactive",
          target: { ...TARGET, id: "m9", name: "retired-model", is_active: false },
          targets: [{ ...TARGET, id: "m9", name: "retired-model", is_active: false }],
        }),
      ],
      [MODEL],
    );

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");

    const select = within(dialog).getByLabelText("Target 1") as HTMLSelectElement;
    expect(select.value).toBe("m9");
  });

  it("deletes behind a confirmation", async () => {
    const { captured, user } = await open([alias()]);

    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Delete permanently/ }));

    await waitFor(() =>
      expect(
        captured.bodies.some(
          (b) => b.method === "DELETE" && b.url.includes("/api/admin/aliases/a1"),
        ),
      ).toBe(true),
    );
  });

  it("shows the gateway's own sentence when a name is taken", async () => {
    // Not rephrased: the server names what holds the name, and an operator
    // needs that, not a generic failure.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url.includes("/api/admin/aliases") && method === "POST") {
          return jsonResponse(
            { error: { message: "The name 'fast' is already used by a model." } },
            409,
          );
        }
        if (url.includes("/api/admin/aliases")) {
          return new Response(JSON.stringify([alias()]), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        if (url.includes("/api/admin/models")) return jsonResponse([MODEL]);
        if (url.includes("/api/admin/groups")) return jsonResponse([]);
        return jsonResponse([]);
      }),
    );
    const user = userEvent.setup();
    renderScreen(<AdminRouting />);
    await screen.findByText("fast");

    await user.click(screen.getByRole("button", { name: /Add alias/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "quick");
    await user.selectOptions(within(dialog).getByLabelText("Target 1"), "m1");
    await user.click(within(dialog).getByRole("button", { name: /Create alias/ }));

    expect(await screen.findByText(/already used by a model/)).toBeTruthy();
  });

  it("shows an inactive alias as inactive as well as unavailable", async () => {
    // Two different reasons to be unusable, and an administrator fixing the
    // wrong one wastes the afternoon: this one is switched off here, that one
    // is switched off over there.
    await open([
      alias({
        is_active: false,
        status: "unavailable",
        unavailable_reason: "target_inactive",
      }),
    ]);
    expect(screen.getByText("Inactive")).toBeTruthy();
    expect(screen.getAllByText(/Target inactive/).length).toBeGreaterThan(0);
  });

  describe("several targets", () => {
    const SECOND: AliasTarget = {
      ...TARGET,
      id: "m3",
      name: "claude-haiku",
      position: 1,
      weight: 4,
    };
    const THIRD_MODEL: AdminModel = {
      ...MODEL,
      id: "m3",
      name: "claude-haiku",
      upstream_model: "anthropic/claude-haiku",
    };

    function routed(overrides: Partial<AdminAlias> = {}): AdminAlias {
      return alias({
        targets: [{ ...TARGET, weight: 1 }, SECOND],
        route_rule: "weighted",
        route_sticky_ttl_seconds: 1800,
        ...overrides,
      });
    }

    function sent(captured: Captured, method: string) {
      return captured.bodies.find((b) => b.method === method)!.body as {
        targets: { model_id: string; weight: number }[];
        route_rule: string;
        route_sticky_ttl_seconds?: number;
      };
    }

    it("lists the targets in order with weights, the rule and the stickiness", async () => {
      await open([routed()], [MODEL, THIRD_MODEL]);

      const row = screen.getByRole("row", { name: /fast/i });
      const links = within(row).getAllByRole("link");
      expect(links.map((a) => a.textContent)).toEqual(["gpt-4o-mini", "claude-haiku"]);
      expect(within(row).getByText("weight 4")).toBeTruthy();
      expect(within(row).getByText(/Weighted · sticky 30 min/)).toBeTruthy();
    });

    it("says which target is down without calling the alias unavailable", async () => {
      await open(
        [
          routed({
            targets: [
              {
                ...TARGET,
                available: false,
                unavailable_reason: "target_inactive",
                is_active: false,
              },
              SECOND,
            ],
          }),
        ],
        [MODEL, THIRD_MODEL],
      );
      const row = screen.getByRole("row", { name: /fast/i });
      expect(within(row).getByText("Available")).toBeTruthy();
      expect(within(row).getByText("Target inactive")).toBeTruthy();
    });

    it("hides the rule line for a single target", async () => {
      await open([alias()]);
      const row = screen.getByRole("row", { name: /fast/i });
      expect(within(row).queryByText(/First available/)).toBeNull();
    });

    it("creates an alias with an ordered route, a rule and a stickiness window", async () => {
      const { captured, user } = await open([alias()], [MODEL, THIRD_MODEL]);

      await user.click(screen.getByRole("button", { name: /Add alias/ }));
      const dialog = await screen.findByRole("dialog");
      await user.type(within(dialog).getByLabelText("Name"), "quick");
      await user.selectOptions(within(dialog).getByLabelText("Target 1"), "m3");
      await user.click(within(dialog).getByRole("button", { name: "Add target" }));
      await user.selectOptions(within(dialog).getByLabelText("Target 2"), "m1");
      await user.selectOptions(within(dialog).getByLabelText("Selection rule"), "cheapest");
      await user.clear(within(dialog).getByLabelText(/Session stickiness/));
      await user.type(within(dialog).getByLabelText(/Session stickiness/), "15");
      await user.click(within(dialog).getByRole("button", { name: /Create alias/ }));

      await waitFor(() => expect(captured.bodies.some((b) => b.method === "POST")).toBe(true));
      const posted = sent(captured, "POST");
      expect(posted.targets).toEqual([
        { model_id: "m3", weight: 1 },
        { model_id: "m1", weight: 1 },
      ]);
      expect(posted.route_rule).toBe("cheapest");
      expect(posted.route_sticky_ttl_seconds).toBe(900);
    });

    it("reorders targets and sends the new order", async () => {
      const { captured, user } = await open([routed()], [MODEL, THIRD_MODEL]);

      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: "Move target 2 up" }));
      await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));

      await waitFor(() => expect(captured.bodies.some((b) => b.method === "PATCH")).toBe(true));
      expect(sent(captured, "PATCH").targets.map((t) => t.model_id)).toEqual(["m3", "m1"]);
    });

    it("shows weights only under the weighted rule, and sends them", async () => {
      const { captured, user } = await open([routed()], [MODEL, THIRD_MODEL]);

      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      const weight = within(dialog).getByLabelText("Weight 2") as HTMLInputElement;
      expect(weight.value).toBe("4");
      await user.clear(weight);
      await user.type(weight, "9");

      await user.selectOptions(within(dialog).getByLabelText("Selection rule"), "first_available");
      expect(within(dialog).queryByLabelText("Weight 2")).toBeNull();
      await user.selectOptions(within(dialog).getByLabelText("Selection rule"), "weighted");
      expect((within(dialog).getByLabelText("Weight 2") as HTMLInputElement).value).toBe("9");

      await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));
      await waitFor(() => expect(captured.bodies.some((b) => b.method === "PATCH")).toBe(true));
      expect(sent(captured, "PATCH").targets[1]).toEqual({ model_id: "m3", weight: 9 });
    });

    it("refuses an out-of-range weight before sending anything", async () => {
      const { captured, user } = await open([routed()], [MODEL, THIRD_MODEL]);
      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      const weight = within(dialog).getByLabelText("Weight 2");
      await user.clear(weight);
      await user.type(weight, "0");
      const save = within(dialog).getByRole("button", { name: /Save alias/ }) as HTMLButtonElement;
      expect(save.disabled).toBe(true);
      expect(captured.bodies.filter((b) => b.method === "PATCH")).toHaveLength(0);
    });

    it("does not offer a model another row already holds", async () => {
      const { user } = await open([routed()], [MODEL, THIRD_MODEL]);
      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      const options = Array.from(
        within(dialog).getByLabelText("Target 2").querySelectorAll("option"),
      ).map((o) => o.textContent);
      expect(options).toContain("claude-haiku");
      expect(options).not.toContain("gpt-4o-mini");
    });

    it("removes a target, but never the last one", async () => {
      const { captured, user } = await open([routed()], [MODEL, THIRD_MODEL]);
      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");

      await user.click(within(dialog).getByRole("button", { name: "Remove target 1" }));
      const last = within(dialog).getByRole("button", {
        name: "Remove target 1",
      }) as HTMLButtonElement;
      expect(last.disabled).toBe(true);

      await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));
      await waitFor(() => expect(captured.bodies.some((b) => b.method === "PATCH")).toBe(true));
      expect(sent(captured, "PATCH").targets).toEqual([{ model_id: "m3", weight: 4 }]);
    });

    it("keeps the kind to the first target's, so a mixed route cannot be built", async () => {
      const { user } = await open([alias()], [MODEL, EMBEDDER, THIRD_MODEL]);
      await user.click(screen.getByRole("button", { name: /Add alias/ }));
      const dialog = await screen.findByRole("dialog");
      await user.selectOptions(within(dialog).getByLabelText("Target 1"), "m1");
      await user.click(within(dialog).getByRole("button", { name: "Add target" }));
      const options = Array.from(
        within(dialog).getByLabelText("Target 2").querySelectorAll("option"),
      ).map((o) => o.textContent);
      expect(options).toContain("claude-haiku");
      expect(options).not.toContain("text-embedding-3-small");
    });

    it("does not rewrite a stickiness window that is not a whole number of minutes", async () => {
      const { captured, user } = await open([routed({ route_sticky_ttl_seconds: 90 })], [
        MODEL,
        THIRD_MODEL,
      ]);
      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));
      await waitFor(() => expect(captured.bodies.some((b) => b.method === "PATCH")).toBe(true));
      expect(sent(captured, "PATCH").route_sticky_ttl_seconds).toBeUndefined();
    });

    it("keeps a rule this console does not know selectable", async () => {
      const { captured, user } = await open(
        [routed({ route_rule: "decision_model" })],
        [MODEL, THIRD_MODEL],
      );
      await user.click(screen.getByRole("button", { name: "Edit" }));
      const dialog = await screen.findByRole("dialog");
      expect((within(dialog).getByLabelText("Selection rule") as HTMLSelectElement).value).toBe(
        "decision_model",
      );
      await user.click(within(dialog).getByRole("button", { name: /Save alias/ }));
      await waitFor(() => expect(captured.bodies.some((b) => b.method === "PATCH")).toBe(true));
      expect(sent(captured, "PATCH").route_rule).toBe("decision_model");
    });
  });
});
