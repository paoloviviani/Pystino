import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  MyRedactionInForce,
  RedactionEvent,
  RedactionTestResult,
  UserRedactionRule,
} from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { MyRedactions } from "./MyRedactions";

/** The member's Redactions page: what the administrators' rules do, the member's own
 *  rules (which can only tighten), the test box, and a log with no values in it. */

const inForce: MyRedactionInForce = {
  engine: "http",
  engine_active: true,
  max_user_rules: 25,
  rules: [
    {
      id: "r1",
      name: "Cloud providers",
      scope: "all",
      subject_label: null,
      default_mode: "off",
      entities: [{ entity_type: "PERSON", mode: "anonymise_restore" }],
      patterns: [{ name: "SECRET_PROJECT", mode: "block" }],
      exceptions: 2,
    },
  ],
};

function rule(overrides: Partial<UserRedactionRule> = {}): UserRedactionRule {
  return {
    id: "u1",
    kind: "mode",
    name: "PERSON",
    regex: null,
    mode: "redact",
    status: "adds_protection",
    admin_mode: "anonymise_restore",
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
    ...overrides,
  };
}

function event(overrides: Partial<RedactionEvent> = {}): RedactionEvent {
  return {
    request_id: "req-1",
    created_at: "2026-11-08T10:00:00Z",
    user_id: null,
    user_label: null,
    model: "gpt-real",
    provider: "openrouter",
    byok: false,
    blocked: false,
    entities: [
      {
        type: "PERSON",
        count: 2,
        mode: "anonymise_restore",
        rule: { id: "r1", name: "Cloud providers", owner: "admin" },
        sources: ["message", "tool_result"],
      },
    ],
    ...overrides,
  };
}

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function api(
  seen: Seen[],
  options: {
    inForce?: MyRedactionInForce;
    rules?: UserRedactionRule[];
    events?: RedactionEvent[];
    test?: RedactionTestResult;
    saveError?: string;
  } = {},
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/in-force")) return jsonResponse(options.inForce ?? inForce);
    if (url.includes("/rules") && method === "GET") {
      const items = options.rules ?? [];
      return jsonResponse({ items, total: items.length, limit: 200, offset: 0 });
    }
    if (url.includes("/rules") && method === "DELETE") return new Response(null, { status: 204 });
    if (url.includes("/rules")) {
      if (options.saveError) {
        return jsonResponse({ error: { message: options.saveError, code: "invalid_rule" } }, 400);
      }
      return jsonResponse(rule());
    }
    if (url.endsWith("/test")) {
      return jsonResponse(
        options.test ?? { engine_active: true, spans: [], blocked: false, note: null },
      );
    }
    if (url.includes("/events")) {
      const items = options.events ?? [];
      return jsonResponse({ items, total: items.length, limit: 25, offset: 0 });
    }
    return jsonResponse({});
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MyRedactions />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("My redactions", () => {
  it("shows the administrators' rules read-only, in plain language, without their regexes", async () => {
    vi.stubGlobal("fetch", api([]));
    renderPage();

    expect(await screen.findByText("Cloud providers")).toBeInTheDocument();
    expect(screen.getByText(/Person name: replaced with a stand-in before it leaves/)).toBeInTheDocument();
    expect(screen.getByText(/Anything matching the pattern SECRET_PROJECT: the request is refused/)).toBeInTheDocument();
    expect(screen.getByText(/2 values are never hidden/)).toBeInTheDocument();
    // Read-only: nothing here edits an administrator's rule.
    const card = screen.getByText("Rules set by administrators").closest("section, div") as HTMLElement;
    expect(within(card).queryByRole("button", { name: /edit|remove/i })).toBeNull();
  });

  it("says nothing is hidden when redaction is switched off", async () => {
    vi.stubGlobal("fetch", api([], { inForce: { ...inForce, engine: "noop", engine_active: false } }));
    renderPage();

    expect(await screen.findByText("Redaction is switched off")).toBeInTheDocument();
  });

  it("lists the user's own rules and says when one adds nothing", async () => {
    vi.stubGlobal(
      "fetch",
      api([], {
        rules: [
          rule(),
          rule({ id: "u2", name: "URL", status: "covered", mode: "anonymise", admin_mode: "block" }),
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText("Adds protection")).toBeInTheDocument();
    expect(screen.getByText("Already covered")).toBeInTheDocument();
    expect(screen.getByText(/already does the same or more \(Block\)/)).toBeInTheDocument();
  });

  it("offers no way to switch protection off or add an exception", async () => {
    vi.stubGlobal("fetch", api([]));
    renderPage();
    await screen.findByText("Add a rule");

    const modes = screen.getByLabelText("What happens to it") as HTMLSelectElement;
    expect([...modes.options].map((o) => o.value)).toEqual([
      "anonymise_restore",
      "anonymise",
      "redact",
      "block",
    ]);
    expect(screen.queryByLabelText(/threshold|exception|allow/i)).toBeNull();
  });

  it("adds a pattern and sends exactly what was typed", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Add a rule");

    await user.selectOptions(screen.getByLabelText("Kind of rule"), "pattern");
    await user.type(screen.getByLabelText("Name of the pattern"), "PROJECT");
    await user.type(screen.getByLabelText("Pattern (regular expression)"), "PRJ-\\d+");
    await user.selectOptions(screen.getByLabelText("What happens to it"), "block");
    await user.click(screen.getByRole("button", { name: "Add rule" }));

    await waitFor(() => expect(seen.some((call) => call.method === "POST")).toBe(true));
    const post = seen.find((call) => call.method === "POST");
    expect(post?.url).toBe("/api/me/redaction/rules");
    expect(post?.body).toEqual({
      kind: "pattern",
      name: "PROJECT",
      mode: "block",
      regex: "PRJ-\\d+",
    });
  });

  it("shows the gateway's refusal of a pattern, in its own words", async () => {
    vi.stubGlobal("fetch", api([], { saveError: "this pattern cannot be used: no lookahead" }));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Add a rule");

    await user.type(screen.getByLabelText("Kind of value"), "PERSON");
    await user.click(screen.getByRole("button", { name: "Add rule" }));

    expect(await screen.findByText("this pattern cannot be used: no lookahead")).toBeInTheDocument();
  });

  it("removes a rule", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen, { rules: [rule()] }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Remove the rule PERSON" }));

    await waitFor(() =>
      expect(seen.some((call) => call.method === "DELETE" && call.url.endsWith("/rules/u1"))).toBe(true),
    );
  });

  it("tests a text and says which rule decided, without echoing anything the server did not match", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal(
      "fetch",
      api(seen, {
        test: {
          engine_active: true,
          blocked: false,
          note: null,
          spans: [
            {
              entity_type: "PERSON",
              start: 6,
              end: 17,
              mode: "anonymise_restore",
              rule: { id: "r1", name: "Cloud providers", owner: "admin" },
            },
            {
              entity_type: "PROJECT",
              start: 21,
              end: 27,
              mode: "redact",
              rule: { id: "u9", name: "PROJECT", owner: "user" },
            },
          ],
        },
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.type(
      await screen.findByLabelText("Text to try"),
      "hello Mario Rossi, PRJ-42 is late",
    );
    await user.click(screen.getByRole("button", { name: "Check this text" }));

    expect(await screen.findByText("Decided by the administrators' rule Cloud providers.")).toBeInTheDocument();
    expect(screen.getByText("Decided by your rule PROJECT.")).toBeInTheDocument();
    const post = seen.find((call) => call.url.endsWith("/test"));
    expect(post?.body).toEqual({ text: "hello Mario Rossi, PRJ-42 is late" });
  });

  it("reports a blocked test as a result, not an error", async () => {
    vi.stubGlobal(
      "fetch",
      api([], { test: { engine_active: true, blocked: true, spans: [], note: null } }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.type(await screen.findByLabelText("Text to try"), "x");
    await user.click(screen.getByRole("button", { name: "Check this text" }));

    expect(await screen.findByText("This would be refused")).toBeInTheDocument();
  });

  it("lists what was hidden, by rule, with no values", async () => {
    vi.stubGlobal(
      "fetch",
      api([], {
        events: [
          event(),
          event({
            request_id: "req-2",
            blocked: true,
            byok: true,
            entities: [
              {
                type: "US_SSN",
                count: 1,
                mode: "block",
                rule: { id: "u1", name: "US_SSN", owner: "user" },
                sources: ["message"],
              },
            ],
          }),
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText("req-1")).toBeInTheDocument();
    expect(screen.getByText("Person name × 2 (Anonymise, restore in the answer)")).toBeInTheDocument();
    expect(screen.getByText("Administrators: Cloud providers")).toBeInTheDocument();
    expect(screen.getByText("Your rule: US_SSN")).toBeInTheDocument();
    expect(screen.getByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("Own key")).toBeInTheDocument();
  });

  it("filters the log by request id", async () => {
    const seen: Seen[] = [];
    vi.stubGlobal("fetch", api(seen, { events: [event()] }));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("req-1");

    await user.type(screen.getByLabelText("Find one request"), "req-1");

    await waitFor(() =>
      expect(seen.some((call) => call.url.includes("/events") && call.url.includes("request_id=req-1"))).toBe(
        true,
      ),
    );
  });
});
