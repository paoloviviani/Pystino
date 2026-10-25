import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedactionPolicy, RedactionRule } from "../lib/types";
import { AdminRedactionRule } from "./AdminRedactionRule";

/**
 * One rule, on its own page (ADR 0039).
 *
 * The properties here are the ones a wrong answer makes invisible: a policy that
 * silently drops a capability the engine reported, a pattern whose refusal is
 * the browser's opinion rather than the server's, a subject that travels with
 * an edit only when it actually changed, and a clone that arrives as a draft —
 * policy copied, subject unset, inactive until a person has looked at it.
 */

function rule(overrides: Partial<RedactionRule> = {}): RedactionRule {
  return {
    id: "r1",
    name: "research group",
    scope: "group",
    scope_id: "g1",
    subject_label: "research",
    policy: {
      default_mode: "off",
      entities: { PERSON: { mode: "anonymise_restore", threshold: null } },
      patterns: [],
      allow_list: [],
    },
    is_active: true,
    reason: "",
    created_by: null,
    created_by_email: null,
    created_at: "2026-08-27T10:00:00Z",
    updated_at: "2026-08-27T10:00:00Z",
    ...overrides,
  };
}

const GROUPS = [
  { id: "g1", name: "research" },
  { id: "g2", name: "clinical" },
];

const STATUS = {
  engine: "http",
  enabled: true,
  endpoint: "http://redaction:8080",
  installed_engines: ["noop", "http"],
  engines: [],
  source: "environment",
  configured: null,
  propagation_seconds: 10,
  fail_open: false,
  restore_in_response: true,
  language: "en",
  score_threshold: 0.5,
  entity_types: null,
  policy: { default_mode: "off", entities: {}, patterns: [], allow_list: [] },
  policy_source: "environment",
  timeout_seconds: 5,
  cache_size: 2048,
  placeholder_key_set: true,
  service: {
    reachable: true,
    detail: "ok",
    latency_ms: 4,
    engine: "presidio",
    engine_version: "2.2.364",
    languages: ["en"],
    models: { en: "en_core_web_lg" },
    degraded_languages: [],
    entities: ["PERSON", "EMAIL_ADDRESS", "URL"],
    pattern_entities: ["EMAIL_ADDRESS", "URL"],
    model_entities: ["PERSON"],
    family_partition: true,
  },
  activity: {
    window_seconds: 86400,
    requests: 0,
    requests_redacted: 0,
    entities_redacted: 0,
    engines: [],
  },
  warnings: [],
};

interface Captured {
  posts: unknown[];
  patches: unknown[];
  /** Bodies sent to the preview endpoint, which is a POST but not a write. */
  previews?: Record<string, unknown>[];
}

/** What the gateway answers a preview with, unless a test says otherwise. */
const PREVIEW = {
  blocked: false,
  blocked_reason: null,
  engine: "http",
  entity_count: 1,
  note: null,
  policy: { default_mode: "off", entities: {}, patterns: [], allow_list: [] },
  redacted_text: "write to <PERSON> at mario@example.com",
  rule_id: null,
  scope: null,
  spans: [
    {
      entity_type: "PERSON",
      start: 9,
      end: 20,
      score: 0.9,
      mode: "redact",
      threshold: 0.5,
      allow_listed: false,
    },
    {
      entity_type: "EMAIL_ADDRESS",
      start: 24,
      end: 41,
      score: 1,
      mode: "off",
      threshold: 0.5,
      allow_listed: false,
    },
  ],
};

function routes(
  rules: RedactionRule[],
  captured: Captured = { posts: [], patches: [] },
  onWrite?: () => Response,
  statusPayload = STATUS,
  groups: { id: string; name: string }[] = GROUPS,
  previewResponse: () => Response = () =>
    new Response(JSON.stringify(PREVIEW), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method === "POST" && url.includes("/redaction/preview")) {
      (captured.previews ??= []).push(JSON.parse(String(init?.body)));
      return previewResponse();
    }
    if (method === "POST" || method === "PATCH") {
      const body = JSON.parse(String(init?.body));
      (method === "POST" ? captured.posts : captured.patches).push(body);
      if (onWrite) return onWrite();
      return new Response(JSON.stringify(rules[0] ?? rule()), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }
    const payload = url.includes("/api/admin/groups")
      ? { items: groups, total: groups.length, limit: 50, offset: 0 }
      : url.includes("/redaction/rules")
        ? { items: rules, total: rules.length, limit: 200, offset: 0 }
        : url.includes("/redaction")
          ? statusPayload
          : { items: [], total: 0, limit: 200, offset: 0 };
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
}

function renderPage(path: string, state?: unknown) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[state === undefined ? path : { pathname: path, state }]}>
        <Routes>
          <Route path="/admin/redaction/rules/:ruleId" element={<AdminRedactionRule />} />
          <Route path="/admin/redaction" element={<div>redaction screen</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminRedactionRule", () => {
  it("offers every scope, including the catch-all", async () => {
    // The catch-all is a scope like any other since ADR 0039 — that is the whole
    // point of it. A screen that omitted it would leave the deployment-wide
    // policy unreachable, which is what the previous shape did by accident.
    vi.stubGlobal("fetch", routes([]));
    renderPage("/admin/redaction/rules/new");

    const scope = await screen.findByLabelText("Scope");
    const options = within(scope)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["Every request", "Provider", "Model", "Group", "User", "API key"]);
  });

  it("offers only the detector's effective entity types", async () => {
    // The API narrows `service.entities` to the enabled recognizer families.
    // A rule editor that merged in a hardcoded NER list would offer PERSON
    // after NER was switched off, and the resulting rule could never fire.
    vi.stubGlobal(
      "fetch",
      routes([], { posts: [], patches: [] }, undefined, {
        ...STATUS,
        service: { ...STATUS.service, entities: ["EMAIL_ADDRESS"] },
      }),
    );
    renderPage("/admin/redaction/rules/new");

    const add = await screen.findByLabelText("Add a kind of data");
    await waitFor(() =>
      expect(
        within(add)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Choose a kind…", "Email address (EMAIL_ADDRESS)"]),
    );
  });

  it("shows a type's row only once the rule says something about it", async () => {
    // A new rule says nothing, so it lists nothing: every type follows the
    // default, and a table of thirty rows that all read "Off" is noise.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([]));
    renderPage("/admin/redaction/rules/new");

    expect(
      await screen.findByText(/Every kind of data follows the default above/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /PERSON/ })).not.toBeInTheDocument();

    const add = await screen.findByLabelText("Add a kind of data");
    await waitFor(() => expect(within(add).getAllByRole("option")).toHaveLength(4));
    await user.selectOptions(add, "PERSON");

    // Stricter than the default, so the row is a decision and not a repeat.
    const person = screen.getByRole("group", { name: /PERSON/ });
    expect(within(person).getByRole("radio", { name: "Redact" })).toBeChecked();
    // Listed rows leave the "add" list.
    expect(within(add).queryByRole("option", { name: /Person name/ })).not.toBeInTheDocument();
    // Plain words first, the id kept for experts.
    expect(screen.getByText("Person name")).toBeInTheDocument();
  });

  it("resets a type to the default by removing its row", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([rule()], captured));
    renderPage("/admin/redaction/rules/r1");

    await screen.findByRole("group", { name: /PERSON/ });
    await user.click(screen.getByRole("button", { name: "Reset Person name to the default" }));
    expect(screen.queryByRole("group", { name: /PERSON/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(captured.patches).toHaveLength(1));
    const body = captured.patches[0] as { policy: RedactionPolicy };
    expect(body.policy.entities).toEqual({});
  });

  it("creates a catch-all rule with no subject", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([], captured));
    renderPage("/admin/redaction/rules/new");

    await screen.findByLabelText("Scope");
    // "Every request" is the default selection, so this asserts it is possible
    // to create the deployment-wide rule without touching a subject control.
    expect(screen.getByText("Applies to every request.")).toBeInTheDocument();

    // One entity turned on, so the rule says something. Each type offers all
    // five modes as a segmented control, not a checkbox: "redact or not" has
    // not been the question since ADR 0037.
    // Awaited: the entity list comes from the detector's own report, which is a
    // second request and may not have landed when the scope control has.
    const add = await screen.findByLabelText("Add a kind of data");
    await waitFor(() => expect(within(add).getAllByRole("option")).toHaveLength(4));
    await user.selectOptions(add, "PERSON");
    const person = screen.getByRole("group", { name: /PERSON/ });
    await user.click(within(person).getByRole("radio", { name: "Restore" }));
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    const body = captured.posts[0] as Record<string, unknown>;
    expect(body.scope).toBe("all");
    expect(body.scope_id).toBeNull();
    expect((body.policy as RedactionPolicy).entities).toEqual({
      PERSON: { mode: "anonymise_restore", threshold: null },
    });
  });

  it("starts a new rule with no patterns", async () => {
    // Templates are offered, not seeded: an empty pattern list is now an
    // explicit starting point, while adding a credential shape remains an
    // explicit choice in the template dialogue.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([], captured));
    renderPage("/admin/redaction/rules/new");

    await screen.findByLabelText("Scope");
    expect(screen.queryByLabelText("Pattern 1 name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    const policy = (captured.posts[0] as { policy: RedactionPolicy }).policy;
    expect(policy.patterns).toEqual([]);
  });

  it("adds a credential pattern from a template", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([], captured));
    renderPage("/admin/redaction/rules/new");

    await screen.findByLabelText("Scope");
    await user.click(screen.getByRole("button", { name: "Add pattern from template" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add OPENAI_KEY pattern" }));
    expect(within(dialog).getByRole("button", { name: "Add OPENAI_KEY pattern" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Done" }));

    await user.click(screen.getByRole("button", { name: "Create rule" }));
    await waitFor(() => expect(captured.posts).toHaveLength(1));
    const policy = (captured.posts[0] as { policy: RedactionPolicy }).policy;
    expect(policy.patterns).toEqual([
      { name: "OPENAI_KEY", regex: "sk-[A-Za-z0-9_-]{16,}", mode: "block" },
    ]);
  });

  it("will not create a scoped rule without a subject", async () => {
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal("fetch", routes([]));
    renderPage("/admin/redaction/rules/new");

    await user.selectOptions(await screen.findByLabelText("Scope"), "group");

    expect(screen.getByRole("button", { name: "Create rule" })).toBeDisabled();
  });

  it("loads an existing rule with its subject editable and seeded", async () => {
    // The freeze is gone: the subject is editable, and the picker arrives
    // already naming the rule's subject rather than blank.
    vi.stubGlobal("fetch", routes([rule()]));
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() => expect(screen.getByLabelText("Scope")).toHaveValue("group"));
    await waitFor(() => expect(screen.getByLabelText("Group")).toHaveValue("g1"));
  });

  it("sends no subject on a save that does not touch it", async () => {
    // The gateway validates a subject it is sent, and a subject may have been
    // deleted since the rule was written. An edit of the policy must not turn
    // into a 404 about a group nobody meant to change.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([rule()], captured));
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() =>
      expect(
        within(screen.getByRole("group", { name: /PERSON/ })).getByRole("radio", {
          name: "Restore",
        }),
      ).toBeChecked(),
    );
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(captured.patches).toHaveLength(1));
    const body = captured.patches[0] as Record<string, unknown>;
    expect(body.scope).toBeUndefined();
    expect(body.scope_id).toBeUndefined();
  });

  it("re-points a rule at another subject of the same kind", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([rule()], captured));
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() => expect(screen.getByLabelText("Group")).toHaveValue("g1"));
    await user.selectOptions(screen.getByLabelText("Group"), "g2");
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(captured.patches).toHaveLength(1));
    const body = captured.patches[0] as Record<string, unknown>;
    expect(body.scope).toBe("group");
    expect(body.scope_id).toBe("g2");
  });

  it("re-scopes a rule to every request", async () => {
    // The repair a dangling rule needs when its subject is gone for good: the
    // catch-all names no row, so it cannot dangle.
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([rule()], captured));
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() => expect(screen.getByLabelText("Scope")).toHaveValue("group"));
    await user.selectOptions(screen.getByLabelText("Scope"), "all");
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(captured.patches).toHaveLength(1));
    const body = captured.patches[0] as Record<string, unknown>;
    expect(body.scope).toBe("all");
    expect(body.scope_id).toBeNull();
  });

  it("names the rule that holds the subject when the move is refused", async () => {
    // The 409 is the server's opinion and names the conflicting rule; the
    // console renders it verbatim instead of guessing at one.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      routes(
        [rule()],
        { posts: [], patches: [] },
        () =>
          new Response(
            JSON.stringify({
              error: {
                message:
                  "A redaction rule already exists for that group (clinical): clinical guard. Edit it, or choose another subject.",
              },
            }),
            { status: 409, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() => expect(screen.getByLabelText("Group")).toHaveValue("g1"));
    await user.selectOptions(screen.getByLabelText("Group"), "g2");
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(screen.getByText(/clinical guard/)).toBeInTheDocument());
    expect(screen.getByText(/already exists for that group/)).toBeInTheDocument();
  });

  it("warns beside a rule whose subject is gone, and offers the repair", async () => {
    // scope_id is not a foreign key, so this rule matches nothing and used to
    // be unrepairable — dead weight with a "deleted subject" label. The warning
    // says so; the editable subject is what makes the warning actionable.
    vi.stubGlobal("fetch", routes([rule({ subject_label: null })]));
    renderPage("/admin/redaction/rules/r1");

    expect(await screen.findByText("This rule's subject no longer exists")).toBeInTheDocument();
    expect(screen.getByText(/scope it to every request/)).toBeInTheDocument();
    // The rule's own subject is still seeded, dangling id and all: the admin
    // may know something the listing does not.
    expect(screen.getByLabelText("Scope")).toHaveValue("group");
  });

  it("clones a rule: policy and reason copied, name gains (copy), subject unset", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([], captured));
    renderPage("/admin/redaction/rules/new", {
      cloneFrom: rule({ reason: "handles patient data" }),
    });

    // Says what a clone is before anything is sent.
    expect(await screen.findByText(/Cloning “research group”/)).toBeInTheDocument();
    expect(screen.getByText(/starts inactive/)).toBeInTheDocument();

    expect(screen.getByLabelText("Rule name")).toHaveValue("research group (copy)");
    expect(screen.getByLabelText("Reason (optional)")).toHaveValue("handles patient data");
    expect(
      await within(screen.getByRole("group", { name: /PERSON/ })).findByRole("radio", {
        name: "Restore",
      }),
    ).toBeChecked();

    // The subject is the one thing that does not come across — the source's is
    // taken, and the catch-all is a choice nobody made by default.
    expect(screen.getByLabelText("Scope")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Create rule" })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText("Scope"), "group");
    await waitFor(() => expect(screen.getByLabelText("Group")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Group"), "g2");
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    const body = captured.posts[0] as Record<string, unknown>;
    // A clone is a draft: inactive until a person has reviewed it, because a
    // redaction rule that switches itself on for an unreviewed subject is a
    // surprise running the wrong way.
    expect(body.is_active).toBe(false);
    expect(body.name).toBe("research group (copy)");
    expect(body.scope).toBe("group");
    expect(body.scope_id).toBe("g2");
    expect((body.policy as RedactionPolicy).entities).toHaveProperty("PERSON");
  });

  it("sends the whole policy on save", async () => {
    const user = userEvent.setup({ delay: null });
    const captured: Captured = { posts: [], patches: [] };
    vi.stubGlobal("fetch", routes([rule()], captured));
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() =>
      expect(
        within(screen.getByRole("group", { name: /PERSON/ })).getByRole("radio", {
          name: "Restore",
        }),
      ).toBeChecked(),
    );
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(captured.patches).toHaveLength(1));
    const body = captured.patches[0] as {
      policy: { entities: Record<string, unknown> };
    };
    expect(body.policy.entities).toHaveProperty("PERSON");
  });

  it("shows the server's refusal rather than guessing at one", async () => {
    // Pattern validity is RE2's opinion, decided server-side. A browser that
    // second-guessed it would disagree with the engine that runs it.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      routes(
        [rule()],
        { posts: [], patches: [] },
        () =>
          new Response(
            JSON.stringify({
              error: { message: "this pattern cannot be used: bad escape" },
            }),
            { status: 400, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    renderPage("/admin/redaction/rules/r1");

    await waitFor(() => expect(screen.getByRole("button", { name: "Save rule" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() =>
      expect(screen.getByText(/this pattern cannot be used/)).toBeInTheDocument(),
    );
  });

  describe("the page", () => {
    it("puts the rule's name first, as the title, with its subject beneath", async () => {
      vi.stubGlobal("fetch", routes([rule()]));
      renderPage("/admin/redaction/rules/r1");

      expect(
        await screen.findByRole("heading", {
          level: 1,
          name: "research group",
        }),
      ).toBeInTheDocument();
      expect(screen.getByText("Applies to group “research”.")).toBeInTheDocument();
      // The first card is the name, the second the subject.
      const cards = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
      expect(cards.slice(0, 3)).toEqual(["Name and reason", "Who it applies to", "What to hide"]);
    });

    it("follows the name as it is typed, and falls back for a new rule", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal("fetch", routes([]));
      renderPage("/admin/redaction/rules/new");

      expect(
        await screen.findByRole("heading", { level: 1, name: "New rule" }),
      ).toBeInTheDocument();
      await user.type(screen.getByLabelText("Rule name"), "Clinical");
      expect(screen.getByRole("heading", { level: 1, name: "Clinical" })).toBeInTheDocument();
    });

    it("calls a clone with no name a clone", async () => {
      vi.stubGlobal("fetch", routes([]));
      renderPage("/admin/redaction/rules/new", {
        cloneFrom: rule({ name: "" }),
      });

      await waitFor(() => expect(screen.getByLabelText("Rule name")).toHaveValue(" (copy)"));
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("(copy)");
    });

    it("keeps the save bar stuck to the bottom, with Back, a hint and Save", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal("fetch", routes([rule()]));
      renderPage("/admin/redaction/rules/r1");

      const bar = await screen.findByRole("region", { name: "Save the rule" });
      expect(bar).toHaveClass("sticky", "bottom-0");
      expect(within(bar).getByRole("button", { name: "Back" })).toBeInTheDocument();
      expect(within(bar).getByRole("button", { name: "Save rule" })).toBeInTheDocument();
      expect(within(bar).getByText("No changes")).toBeInTheDocument();

      await user.type(screen.getByLabelText("Reason (optional)"), "x");
      expect(within(bar).getByText("Unsaved changes")).toBeInTheDocument();
      // Put back, it is the saved rule again.
      await user.clear(screen.getByLabelText("Reason (optional)"));
      expect(within(bar).getByText("No changes")).toBeInTheDocument();
    });

    it("says why Create is disabled on a clone with no subject", async () => {
      vi.stubGlobal("fetch", routes([]));
      renderPage("/admin/redaction/rules/new", { cloneFrom: rule() });

      const bar = await screen.findByRole("region", { name: "Save the rule" });
      expect(within(bar).getByText("Choose who it applies to before saving")).toBeInTheDocument();
      expect(within(bar).getByRole("button", { name: "Create rule" })).toBeDisabled();
    });

    it("goes back from the save bar", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal("fetch", routes([rule()]));
      renderPage("/admin/redaction/rules/r1");

      await user.click(await screen.findByRole("button", { name: "Back" }));
      expect(await screen.findByText("redaction screen")).toBeInTheDocument();
    });

    it("summarises the form as it is edited", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal("fetch", routes([rule()]));
      renderPage("/admin/redaction/rules/r1");

      const summary = await screen.findByTestId("rule-summary");
      await waitFor(() =>
        expect(summary).toHaveTextContent("Anonymises person name and restores it in the answer."),
      );

      await user.click(
        within(screen.getByRole("group", { name: /PERSON/ })).getByRole("radio", { name: "Block" }),
      );
      expect(summary).toHaveTextContent("Blocks person name.");
    });
  });

  describe("the test box", () => {
    it("tests the unsaved policy, not the stored rule", async () => {
      const user = userEvent.setup({ delay: null });
      const captured: Captured = { posts: [], patches: [], previews: [] };
      vi.stubGlobal("fetch", routes([rule()], captured));
      renderPage("/admin/redaction/rules/r1");

      // An edit the stored rule does not have.
      await user.click(
        within(await screen.findByRole("group", { name: /PERSON/ })).getByRole("radio", {
          name: "Block",
        }),
      );
      await user.type(screen.getByLabelText("Sample text"), "write to Mario Rossi");

      await waitFor(() => expect(captured.previews).toHaveLength(1), {
        timeout: 3000,
      });
      // Debounced: one request for the whole sentence, not one per key.
      const sent = captured.previews?.[0] as Record<string, unknown>;
      expect(sent.text).toBe("write to Mario Rossi");
      expect((sent.policy as RedactionPolicy).entities.PERSON?.mode).toBe("block");
      // Tested as the rule's own subject, so the catch-all folds in as it would.
      expect(sent.scope).toBe("group");
      expect(sent.scope_id).toBe("g1");
      // And nothing was saved by testing.
      expect(captured.patches).toHaveLength(0);
    });

    it("shows what the provider receives, with the replaced spans picked out", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal("fetch", routes([rule()]));
      renderPage("/admin/redaction/rules/r1");

      await user.type(
        await screen.findByLabelText("Sample text"),
        "write to Mario Rossi at mario@example.com",
      );

      const output = await screen.findByTestId("rule-test-output", undefined, {
        timeout: 3000,
      });
      expect(output).toHaveTextContent("write to <PERSON> at mario@example.com");
      expect(within(output).getByText("<PERSON>").tagName).toBe("MARK");
      expect(screen.getByText("1 thing replaced")).toBeInTheDocument();
      // The e-mail was found and the rule leaves it: said, not hidden.
      expect(screen.getByText("Found but left as written: Email address")).toBeInTheDocument();
    });

    it("sends the catch-all as a draft with no subject", async () => {
      const user = userEvent.setup({ delay: null });
      const captured: Captured = { posts: [], patches: [], previews: [] };
      vi.stubGlobal("fetch", routes([], captured));
      renderPage("/admin/redaction/rules/new");

      await user.type(await screen.findByLabelText("Sample text"), "hello");
      await waitFor(() => expect(captured.previews).toHaveLength(1), {
        timeout: 3000,
      });
      expect(captured.previews?.[0]).toMatchObject({ scope: "all" });
      expect(captured.previews?.[0]).not.toHaveProperty("scope_id");
    });

    it("says a block is a block", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal(
        "fetch",
        routes(
          [rule()],
          undefined,
          undefined,
          STATUS,
          GROUPS,
          () =>
            new Response(
              JSON.stringify({
                ...PREVIEW,
                blocked: true,
                blocked_reason: "US_SSN",
                redacted_text: null,
                entity_count: 0,
              }),
              { status: 200, headers: { "content-type": "application/json" } },
            ),
        ),
      );
      renderPage("/admin/redaction/rules/r1");

      await user.type(await screen.findByLabelText("Sample text"), "123-45-6789");
      expect(
        await screen.findByText(
          "This text would be refused before it reached a provider.",
          undefined,
          { timeout: 3000 },
        ),
      ).toBeInTheDocument();
      expect(screen.getByText("US_SSN")).toBeInTheDocument();
    });

    it("shows the gateway's refusal plainly", async () => {
      const user = userEvent.setup({ delay: null });
      vi.stubGlobal(
        "fetch",
        routes(
          [rule()],
          undefined,
          undefined,
          STATUS,
          GROUPS,
          () =>
            new Response(
              JSON.stringify({
                error: { message: "the redaction service is not reachable" },
              }),
              {
                status: 502,
                headers: { "content-type": "application/json" },
              },
            ),
        ),
      );
      renderPage("/admin/redaction/rules/r1");

      await user.type(await screen.findByLabelText("Sample text"), "hello");
      expect(
        await screen.findByText(/the redaction service is not reachable/, undefined, {
          timeout: 3000,
        }),
      ).toBeInTheDocument();
      expect(screen.getByText("The test could not run")).toBeInTheDocument();
    });

    it("sends nothing while the box is empty", async () => {
      const captured: Captured = { posts: [], patches: [], previews: [] };
      vi.stubGlobal("fetch", routes([rule()], captured));
      renderPage("/admin/redaction/rules/r1");

      await screen.findByLabelText("Sample text");
      await new Promise((resolve) => setTimeout(resolve, 700));
      expect(captured.previews).toHaveLength(0);
    });
  });

  describe("the allowlist", () => {
    const listed = rule({
      policy: {
        default_mode: "redact",
        entities: {},
        patterns: [],
        allow_list: ["example.org"],
      },
    });
    const catchAll = (overrides: Partial<RedactionRule> = {}) =>
      rule({ id: "r0", name: "everyone", scope: "all", scope_id: null, ...overrides });

    it("is ignored, and the page says so, when a catch-all rule exists", async () => {
      vi.stubGlobal("fetch", routes([listed, catchAll()]));
      renderPage("/admin/redaction/rules/r1");

      const summary = await screen.findByTestId("rule-summary");
      await waitFor(() => expect(summary).toHaveTextContent("ignores its never-hide list"));
      expect(summary).not.toHaveTextContent("never hides");
      expect(screen.getByText(/This list is ignored/)).toBeInTheDocument();
    });

    it("counts when there is no catch-all rule", async () => {
      vi.stubGlobal("fetch", routes([listed]));
      renderPage("/admin/redaction/rules/r1");

      const summary = await screen.findByTestId("rule-summary");
      await waitFor(() => expect(summary).toHaveTextContent("never hides 1 listed value"));
      expect(screen.queryByText(/This list is ignored/)).not.toBeInTheDocument();
    });

    it("counts when the only catch-all is inactive, or is the rule itself", async () => {
      vi.stubGlobal("fetch", routes([listed, catchAll({ is_active: false })]));
      const first = renderPage("/admin/redaction/rules/r1");
      await waitFor(() =>
        expect(screen.getByTestId("rule-summary")).toHaveTextContent("never hides 1 listed value"),
      );
      first.unmount();

      vi.stubGlobal("fetch", routes([catchAll({ policy: listed.policy })]));
      renderPage("/admin/redaction/rules/r0");
      await waitFor(() =>
        expect(screen.getByTestId("rule-summary")).toHaveTextContent("never hides 1 listed value"),
      );
    });
  });

  it("says so when the rule is gone", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderPage("/admin/redaction/rules/missing");

    await waitFor(() => expect(screen.getByText("No such rule")).toBeInTheDocument());
  });
});
