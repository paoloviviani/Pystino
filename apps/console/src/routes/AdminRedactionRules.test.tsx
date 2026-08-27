import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedactionRule } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminRedactionRules } from "./AdminRedactionRules";

/**
 * The screen exists to answer "who is treated differently, and how".
 *
 * What is pinned is the pair of facts a row cannot be read without: the subject
 * a rule attaches to — including that it has been deleted, which makes the rule
 * inert while looking identical to one that works — and what the policy on it
 * actually says.
 */

function rule(overrides: Partial<RedactionRule> = {}): RedactionRule {
  return {
    id: "r1",
    name: "clinical group",
    scope: "group",
    scope_id: "g1",
    subject_label: "clinical",
    policy: {
      default_mode: "anonymise",
      entities: { PERSON: { mode: "block", threshold: null } },
      patterns: [],
      allow_list: [],
    },
    is_active: true,
    reason: "ethics approval 2026-14",
    created_by: "u1",
    created_by_email: "dave@example.org",
    created_at: "2026-08-20T10:00:00Z",
    updated_at: "2026-08-20T10:00:00Z",
    ...overrides,
  };
}

interface Calls {
  posts: unknown[];
  patches: { url: string; body: unknown }[];
  deletes: string[];
}

/**
 * The rules listing, and a record of everything written to it.
 *
 * The group listing is answered too, because the create dialog's subject picker
 * fetches one — a dialog that offers no subjects cannot create a rule, and the
 * point of the test is what the API is asked for.
 */
function routes(rules: RedactionRule[], calls: Calls): typeof fetch {
  const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url.includes("/redaction/rules")) {
      if (method === "POST") {
        calls.posts.push(JSON.parse(String(init?.body)));
        return jsonResponse(rule({ id: "new" }), 201);
      }
      if (method === "PATCH") {
        calls.patches.push({ url, body: JSON.parse(String(init?.body)) });
        return jsonResponse(rule({ is_active: false }));
      }
      if (method === "DELETE") {
        calls.deletes.push(url.split("/").pop() ?? "");
        return new Response(null, { status: 204 });
      }
      return jsonResponse(rules);
    }

    if (url.includes("/api/admin/groups")) {
      return jsonResponse([{ id: "g1", name: "clinical", description: null }]);
    }

    // The redaction status, for the entity rows in the policy form.
    return jsonResponse({
      engine: "http",
      score_threshold: 0.5,
      service: { entities: ["PERSON", "EMAIL_ADDRESS"] },
    });
  });
  return stub as unknown as typeof fetch;
}

function noCalls(): Calls {
  return { posts: [], patches: [], deletes: [] };
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

describe("AdminRedactionRules", () => {
  it("lists a rule with its subject, its policy and its state", async () => {
    vi.stubGlobal("fetch", routes([rule()], noCalls()));
    renderScreen(<AdminRedactionRules />);

    await waitFor(() => expect(screen.getByText("clinical group")).toBeInTheDocument());
    expect(screen.getByText(/Group · clinical/)).toBeInTheDocument();
    // The policy in one line: a scope whose summary said only "group" would
    // need opening to learn whether it does anything.
    expect(screen.getByText("Anonymise by default · 1 type")).toBeInTheDocument();
    // Scoped to the table: "Active" is also one of the filter's options.
    expect(within(screen.getByRole("table")).getByText("Active")).toBeInTheDocument();
  });

  it("says when a rule's subject has been deleted", async () => {
    // Null is not a missing name. The rule matches nothing, and nothing else on
    // the screen distinguishes it from one that is working — scope_id is not a
    // foreign key, so the database does not catch it either.
    vi.stubGlobal("fetch", routes([rule({ subject_label: null })], noCalls()));
    renderScreen(<AdminRedactionRules />);

    await waitFor(() => expect(screen.getByText("Subject deleted")).toBeInTheDocument());
  });

  it("creates a rule for a chosen subject", async () => {
    const calls = noCalls();
    vi.stubGlobal("fetch", routes([], calls));
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "New rule" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.type(dialog.getByLabelText("Name"), "clinical group");
    await waitFor(() => expect(dialog.getByLabelText("Group")).toBeInTheDocument());
    await user.selectOptions(dialog.getByLabelText("Group"), "g1");
    await user.selectOptions(dialog.getByLabelText("Default mode"), "anonymise");
    await user.selectOptions(dialog.getByLabelText(/PERSON/), "block");
    await user.click(dialog.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(calls.posts).toHaveLength(1));
    expect(calls.posts[0]).toEqual({
      name: "clinical group",
      scope: "group",
      scope_id: "g1",
      policy: {
        default_mode: "anonymise",
        entities: { PERSON: { mode: "block", threshold: null } },
        patterns: [],
        allow_list: [],
      },
      reason: "",
    });
  });

  it("will not send a rule with no subject", async () => {
    // The API refuses it, and a rule that matched nothing would look identical
    // on this screen to one that matched everything.
    const calls = noCalls();
    vi.stubGlobal("fetch", routes([], calls));
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "New rule" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByRole("button", { name: "Create" })).toBeDisabled();
    expect(calls.posts).toHaveLength(0);
  });

  it("deactivates a rule without deleting it", async () => {
    const calls = noCalls();
    vi.stubGlobal("fetch", routes([rule()], calls));
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "Deactivate" }));

    await waitFor(() => expect(calls.patches).toHaveLength(1));
    expect(calls.patches[0]!.body).toEqual({ is_active: false });
    expect(calls.deletes).toHaveLength(0);
  });

  it("deletes a rule", async () => {
    const calls = noCalls();
    vi.stubGlobal("fetch", routes([rule()], calls));
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => expect(calls.deletes).toEqual(["r1"]));
  });

  it("edits a rule's policy without touching its scope", async () => {
    // A rule is a decision about one subject; re-pointing it at another would
    // silently make two subjects' histories read as one, so the API has no way
    // to do it and neither does this form.
    const calls = noCalls();
    vi.stubGlobal("fetch", routes([rule()], calls));
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.queryByLabelText("Scope")).not.toBeInTheDocument();
    await user.selectOptions(dialog.getByLabelText("Default mode"), "redact");
    await user.click(dialog.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(calls.patches).toHaveLength(1));
    const body = calls.patches[0]!.body as { policy: { default_mode: string }; name: string };
    expect(body.policy.default_mode).toBe("redact");
    expect(body.name).toBe("clinical group");
  });

  it("shows the API's refusal when a subject already has a rule", async () => {
    // "Edit it instead of adding a second one" is the operator's next action,
    // and it is the API that knows to say so.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(
            JSON.stringify({
              error: {
                message:
                  "A redaction rule already exists for that group (clinical). Edit it instead of adding a second one.",
              },
            }),
            { status: 409, headers: { "content-type": "application/json" } },
          );
        }
        if (String(input).includes("/api/admin/groups")) {
          return jsonResponse([{ id: "g1", name: "clinical", description: null }]);
        }
        return jsonResponse([]);
      }),
    );
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.click(await screen.findByRole("button", { name: "New rule" }));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByLabelText("Group")).toBeInTheDocument());
    await user.selectOptions(dialog.getByLabelText("Group"), "g1");
    await user.click(dialog.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(screen.getByText(/already exists for that group/)).toBeInTheDocument(),
    );
  });

  it("filters by scope, in the request rather than in the browser", async () => {
    // Paginated in the database, so filtering client-side would hide rows that
    // are on another page.
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        seen.push(String(input));
        return jsonResponse([]);
      }),
    );
    const user = userEvent.setup({ delay: null });
    renderScreen(<AdminRedactionRules />);

    await user.selectOptions(await screen.findByLabelText("Scope"), "model");

    await waitFor(() => expect(seen.some((url) => url.includes("scope=model"))).toBe(true));
  });
});
