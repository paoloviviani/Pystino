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
}

function routes(
  rules: RedactionRule[],
  captured: Captured = { posts: [], patches: [] },
  onWrite?: () => Response,
  statusPayload = STATUS,
  groups: { id: string; name: string }[] = GROUPS,
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
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
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
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
    const options = within(scope).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual([
      "Every request",
      "Provider",
      "Model",
      "Group",
      "User",
      "API key",
    ]);
  });

  it("lists only the detector's effective entity types", async () => {
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

    await screen.findByRole("group", { name: /EMAIL_ADDRESS/ });
    expect(screen.queryByRole("group", { name: /PERSON/ })).not.toBeInTheDocument();
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
    // five modes as radios, not a checkbox: "redact or not" has not been the
    // question since ADR 0037.
    // Awaited: the entity list comes from the detector's own report, which is a
    // second request and may not have landed when the scope control has.
    const person = await screen.findByRole("group", { name: /PERSON/ });
    await user.click(within(person).getByRole("radio", { name: "Restore" }));
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    const body = captured.posts[0] as Record<string, unknown>;
    expect(body.scope).toBe("all");
    expect(body.scope_id).toBeNull();
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
    expect(
      within(dialog).getByRole("button", { name: "Add OPENAI_KEY pattern" }),
    ).toBeDisabled();
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
    renderPage("/admin/redaction/rules/new", { cloneFrom: rule({ reason: "handles patient data" }) });

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
    const body = captured.patches[0] as { policy: { entities: Record<string, unknown> } };
    expect(body.policy.entities).toHaveProperty("PERSON");
  });

  it("shows the server's refusal rather than guessing at one", async () => {
    // Pattern validity is RE2's opinion, decided server-side. A browser that
    // second-guessed it would disagree with the engine that runs it.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      routes([rule()], { posts: [], patches: [] }, () =>
        new Response(
          JSON.stringify({ error: { message: "this pattern cannot be used: bad escape" } }),
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

  it("says so when the rule is gone", async () => {
    vi.stubGlobal("fetch", routes([]));
    renderPage("/admin/redaction/rules/missing");

    await waitFor(() => expect(screen.getByText("No such rule")).toBeInTheDocument());
  });
});
