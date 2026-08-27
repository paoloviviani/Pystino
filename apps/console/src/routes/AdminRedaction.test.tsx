import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedactionEngineOption, RedactionPreview, RedactionStatus } from "../lib/types";
import { AdminRedaction } from "./AdminRedaction";

/**
 * The screen exists to answer "is redaction on, and is it working". So what is
 * pinned is the difference between those two — a layer that is switched on and
 * detecting nothing looks identical to one with nothing to find, and only the
 * entity count and the API's warnings tell them apart.
 */

function engine(
  overrides: Partial<RedactionEngineOption> & { name: string },
): RedactionEngineOption {
  return {
    label: overrides.name,
    description: "Calls an out-of-process detection service.",
    needs_endpoint: true,
    redacts: true,
    is_active: false,
    blocked_reason: null,
    ...overrides,
  };
}

function status(overrides: Partial<RedactionStatus> = {}): RedactionStatus {
  return {
    engine: "http",
    enabled: true,
    endpoint: "http://redaction:8080",
    installed_engines: ["noop", "http"],
    engines: [
      engine({ name: "http", label: "Presidio (detection service)", is_active: true }),
      engine({
        name: "noop",
        label: "Off (noop)",
        description: "Nothing is removed.",
        redacts: false,
      }),
    ],
    source: "environment",
    configured: null,
    propagation_seconds: 10,
    fail_open: false,
    restore_in_response: true,
    language: "en",
    score_threshold: 0.5,
    entity_types: null,
    policy: {
      default_mode: "anonymise_restore",
      entities: { URL: { mode: "off", threshold: null } },
      patterns: [],
      allow_list: [],
    },
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
      entities: ["PERSON", "EMAIL_ADDRESS"],
    },
    activity: {
      window_seconds: 86400,
      requests: 40,
      requests_redacted: 12,
      entities_redacted: 31,
      engines: ["http"],
    },
    warnings: [],
    ...overrides,
  };
}

function respondWith(body: RedactionStatus) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
}

interface Captured {
  puts: { url: string; body: unknown }[];
}

/**
 * A fetch stub that records the PUTs and answers them with *after*.
 *
 * The route returns the whole status document, so the screen updates from the
 * response rather than refetching — which is what this has to model, or the test
 * would pass against a screen that quietly showed stale state.
 */
function withEngineChange(
  before: RedactionStatus,
  after: RedactionStatus,
): { fetch: typeof fetch; captured: Captured } {
  const captured: Captured = { puts: [] };
  const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "PUT") {
      captured.puts.push({ url, body: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify(after), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify(before), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  return { fetch: stub as unknown as typeof fetch, captured };
}


/**
 * A preview result, and a stub that answers the POST with it.
 *
 * Offsets are into the sample the tests type, because that is what the screen
 * slices to show each match — the API reports positions, not text.
 */
function preview(overrides: Partial<RedactionPreview> = {}): RedactionPreview {
  return {
    engine: "http",
    scope: null,
    rule_id: null,
    policy: status().policy,
    spans: [
      {
        entity_type: "PERSON",
        start: 0,
        end: 8,
        score: 0.85,
        mode: "redact",
        threshold: 0.5,
        allow_listed: false,
      },
      {
        entity_type: "URL",
        start: 34,
        end: 43,
        score: 0.6,
        mode: "off",
        threshold: 0.5,
        allow_listed: true,
      },
    ],
    redacted_text: "<PERSON> le notizie del giorno da ilpost.it",
    entity_count: 1,
    blocked: false,
    blocked_reason: null,
    note: null,
    ...overrides,
  };
}

function withPreview(result: RedactionPreview): { fetch: typeof fetch; posts: unknown[] } {
  const posts: unknown[] = [];
  const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes("/api/admin/groups")) {
      return new Response(
        JSON.stringify({
          items: [{ id: "g1", name: "clinical", description: null }],
          total: 1,
          limit: 50,
          offset: 0,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (init?.method === "POST" && String(input).includes("/redaction/preview")) {
      posts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify(status()), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  return { fetch: stub as unknown as typeof fetch, posts };
}

/** The sample the preview tests run, and the prompt the Italian bug came from. */
const SAMPLE = "Riassumi le notizie del giorno da ilpost.it";

function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminRedaction", () => {
  it("says whether redaction is on, and which engine", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());
    expect(screen.getByText("http engine")).toBeInTheDocument();
  });

  it("says plainly when nothing is being redacted", async () => {
    // The commonest surprise: configured, and doing nothing at all.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          engine: "noop",
          enabled: false,
          service: null,
          warnings: ["Redaction is not enabled: the engine is 'noop'."],
        }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Off")).toBeInTheDocument());
    expect(screen.getByText("prompts reach the provider unchanged")).toBeInTheDocument();
    expect(screen.getByText(/is not enabled/)).toBeInTheDocument();
  });

  it("lists every installed engine with what it does, not just its name", async () => {
    // A name is not a choice: "http" and "noop" say nothing about which one
    // leaves personal data in a prompt. The description is what is being chosen.
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() =>
      expect(screen.getByText("Presidio (detection service)")).toBeInTheDocument(),
    );
    expect(screen.getByText("Off (noop)")).toBeInTheDocument();
    expect(screen.getByText("Nothing is removed.")).toBeInTheDocument();
    expect(screen.getByText("In force")).toBeInTheDocument();
    expect(screen.getByText("Redacts nothing")).toBeInTheDocument();
  });

  it("will not offer an engine the API would refuse", async () => {
    // blocked_reason is computed server-side precisely so the button cannot be
    // offered and then rejected.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          engine: "noop",
          enabled: false,
          engines: [
            engine({
              name: "http",
              blocked_reason: "No detection endpoint is configured. Set GATEWAY_REDACTION__ENDPOINT",
            }),
            engine({ name: "noop", redacts: false, is_active: true }),
          ],
        }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText(/Cannot be enabled/)).toBeInTheDocument());
    expect(screen.getByText(/GATEWAY_REDACTION__ENDPOINT/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeDisabled();
  });

  it("enables a redacting engine in one click", async () => {
    // No confirmation for turning protection *on*: a justification prompt with
    // no reader is friction.
    const off = status({
      engine: "noop",
      enabled: false,
      engines: [
        engine({ name: "http", label: "Presidio (detection service)" }),
        engine({ name: "noop", redacts: false, is_active: true }),
      ],
    });
    const { fetch, captured } = withEngineChange(off, status());
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    renderScreen(<AdminRedaction />);

    await user.click(await screen.findByRole("button", { name: "Enable" }));

    await waitFor(() => expect(captured.puts.length).toBe(1));
    expect(captured.puts[0]!.url).toContain("/api/admin/redaction/engine");
    expect(captured.puts[0]!.body).toEqual({ engine: "http", reason: "" });
  });

  it("makes turning redaction off require a written reason", async () => {
    // The one change that makes the system quietly stop protecting anything. A
    // checkbox saying "I understand" would leave no record; this leaves a row.
    const { fetch, captured } = withEngineChange(
      status(),
      status({ engine: "noop", enabled: false }),
    );
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    renderScreen(<AdminRedaction />);

    await user.click(await screen.findByRole("button", { name: "Turn redaction off" }));

    const dialog = within(await screen.findByRole("dialog"));
    // Nothing is sent until a reason is typed.
    expect(dialog.getByRole("button", { name: "Turn it off" })).toBeDisabled();
    expect(captured.puts.length).toBe(0);

    await user.type(dialog.getByLabelText("Reason"), "service migration");
    await user.click(dialog.getByRole("button", { name: "Turn it off" }));

    await waitFor(() => expect(captured.puts.length).toBe(1));
    expect(captured.puts[0]!.body).toEqual({ engine: "noop", reason: "service migration" });
  });

  it("says whether the environment or this console decided the engine", async () => {
    // Otherwise an environment variable that no longer takes effect looks like
    // a broken one.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          source: "console",
          configured: {
            engine: "http",
            reason: "turned back on after the migration",
            changed_at: "2026-08-24T09:00:00Z",
            changed_by: "dave@example.org",
          },
        }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText(/This console/)).toBeInTheDocument());
    expect(screen.getByText("turned back on after the migration")).toBeInTheDocument();
    expect(screen.getByText(/dave@example\.org/)).toBeInTheDocument();
  });

  it("names the environment variable when nothing has been set here", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() =>
      expect(screen.getByText(/GATEWAY_REDACTION__ENGINE/)).toBeInTheDocument(),
    );
  });

  it("reports the detection service as it answered just now", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Answering")).toBeInTheDocument());
    expect(screen.getByText(/presidio 2\.2\.364 · 4ms/)).toBeInTheDocument();
  });

  it("surfaces an unreachable service with the reason", async () => {
    // With fail_open off this means every request is being refused, which is
    // why it is on the page rather than in a log.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          service: {
            ...status().service!,
            reachable: false,
            detail: "could not reach http://redaction:8080: connection refused",
          },
          warnings: ["The detection service is not answering."],
        }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Not answering")).toBeInTheDocument());
    expect(screen.getByText(/connection refused/)).toBeInTheDocument();
  });

  it("shows what redaction has actually removed, not just its settings", async () => {
    // Configuration is not evidence. A wrong language or too high a threshold
    // both look like healthy silence.
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("31")).toBeInTheDocument());
    expect(screen.getByText(/from 12 of 40 requests, last 24 hours/)).toBeInTheDocument();
  });

  it("renders the API's warnings verbatim", async () => {
    const note = "fail_open is on, so a detection failure forwards the prompt unredacted.";
    vi.stubGlobal("fetch", respondWith(status({ fail_open: true, warnings: [note] })));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText(note)).toBeInTheDocument());
    expect(screen.getByText("Forward unredacted")).toBeInTheDocument();
  });

  it("shows the environment's entity list only when it is set", async () => {
    // Null and a list are different facts, and conflating them is the difference
    // between redacting everything and redacting nothing. Since ADR 0037 the
    // policy editor is where that shows; this card only explains where a policy
    // came from when the deprecated variable set it, so it is absent otherwise.
    vi.stubGlobal("fetch", respondWith(status({ entity_types: null })));
    const { unmount } = renderScreen(<AdminRedaction />);
    await waitFor(() => expect(screen.getByText("Redacted entities")).toBeInTheDocument());
    expect(screen.queryByText("Entity types")).not.toBeInTheDocument();
    unmount();

    vi.stubGlobal("fetch", respondWith(status({ entity_types: ["PERSON"] })));
    renderScreen(<AdminRedaction />);
    await waitFor(() => expect(screen.getByText("Entity types")).toBeInTheDocument());
  });

  it("names the model backing the configured language", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText(/en_core_web_lg/)).toBeInTheDocument());
  });

  it("lists degraded languages, which are otherwise invisible", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          service: { ...status().service!, languages: ["en", "it"], degraded_languages: ["it"] },
        }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() =>
      expect(screen.getByText(/Served without a named-entity model/)).toBeInTheDocument(),
    );
    const card = screen.getByText("Degraded languages").closest("section")!;
    expect(within(card).getByText("it")).toBeInTheDocument();
  });

  it("never renders the placeholder key, only whether it is set", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Set")).toBeInTheDocument());
    expect(screen.getByText(/must stay stable/)).toBeInTheDocument();
  });

  it("shows the error the API gave rather than a blank page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { message: "Admin access required." } }), {
            status: 403,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Admin access required.")).toBeInTheDocument());
  });

  it("offers a mode per entity type, from the detector's own list", async () => {
    // Hard-coding the list here would mean a console that silently omits a
    // recogniser the engine gained last week — the failure this feature fixes.
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByLabelText(/PERSON/)).toBeInTheDocument());
    expect(screen.getByLabelText(/EMAIL_ADDRESS/)).toBeInTheDocument();
    // The one the default policy turns off, shown as such rather than hidden.
    expect(screen.getByLabelText(/URL/)).toHaveValue("off");
  });

  it("sends the whole policy, with the allow-list split", async () => {
    const user = userEvent.setup({ delay: null });
    const { fetch: stub, captured } = withEngineChange(status(), status());
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByLabelText(/PERSON/)).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText(/PERSON/), "redact");
    await user.type(screen.getByLabelText("Allowlist"), "ilpost.it, acme.test");
    await user.type(screen.getByLabelText("Change reason"), "URLs are the source");
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(captured.puts).toHaveLength(1));
    const put = captured.puts[0] as { url: string; body: Record<string, any> };
    expect(put.url).toContain("/api/admin/redaction/policy");
    expect(put.body.policy.entities.PERSON.mode).toBe("redact");
    expect(put.body.policy.allow_list).toEqual(["ilpost.it", "acme.test"]);
    expect(put.body.reason).toBe("URLs are the source");
  });

  it("shows the refusal when a weaker policy arrives without a reason", async () => {
    // The rule lives in the API, because "protects less" is a comparison against
    // what is running. The screen's job is to render the sentence, not to
    // re-derive it — two copies of that rule would disagree.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return new Response(
            JSON.stringify({
              error: { message: "this policy protects less than the one in force" },
            }),
            { status: 400, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify(status()), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByLabelText(/PERSON/)).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText(/PERSON/), "off");
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() =>
      expect(screen.getByText(/protects less than the one in force/)).toBeInTheDocument(),
    );
  });

  it("names each entity type in words, and keeps the label the policy stores", async () => {
    // Both, not either: "Person name" is what a person understands and PERSON
    // is what the policy stores, what the ledger counts and what a log is
    // grepped for. A screen showing only one of them cannot be matched against
    // the other.
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() => expect(screen.getByText("Person name")).toBeInTheDocument());
    expect(screen.getByText("PERSON")).toBeInTheDocument();
    expect(screen.getByText("Email address")).toBeInTheDocument();
    expect(screen.getByText("EMAIL_ADDRESS")).toBeInTheDocument();
    // Where the detection comes from: a regex is exact, a model reading is a
    // guess with a score — and it is the guesses that read an Italian verb as
    // a person.
    const person = screen.getByText("PERSON").closest("label")!;
    expect(within(person).getByText("Model")).toBeInTheDocument();
    const email = screen.getByText("EMAIL_ADDRESS").closest("label")!;
    expect(within(email).getByText("Pattern")).toBeInTheDocument();
  });

  it("falls back to the raw label for a recogniser it has never heard of", async () => {
    // The failure this feature exists to fix, one level up: a type nobody can
    // see is a type nobody rules on.
    vi.stubGlobal(
      "fetch",
      respondWith(status({ service: { ...status().service!, entities: ["ZX_NEW_THING"] } })),
    );
    renderScreen(<AdminRedaction />);

    // Exactly the raw label, printed once: nothing here knows this type, and a
    // name invented for it would be worse than none.
    await waitFor(() => expect(screen.getByLabelText("ZX_NEW_THING")).toBeInTheDocument());
  });

  it("adds a custom pattern and sends it with the policy", async () => {
    const user = userEvent.setup({ delay: null });
    const { fetch: stub, captured } = withEngineChange(status(), status());
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.click(await screen.findByRole("button", { name: "Add pattern" }));
    await user.type(screen.getByLabelText("Pattern 1 name"), "TICKET_ID");
    await user.type(screen.getByLabelText("Pattern 1 regex"), "TICKET-\\d+");
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(captured.puts).toHaveLength(1));
    const put = captured.puts[0] as { url: string; body: Record<string, any> };
    expect(put.url).toContain("/api/admin/redaction/policy");
    expect(put.body.policy.patterns).toEqual([
      // Redact by default: a pattern somebody wrote by hand names a value they
      // went out of their way to name.
      { name: "TICKET_ID", regex: "TICKET-\\d+", mode: "redact" },
    ]);
  });

  it("shows the API's message when a pattern will not compile", async () => {
    // RE2 refuses constructs JavaScript accepts — backreferences, lookaround —
    // so validating in the browser would pass patterns the API then rejects.
    // Its parser names the construct, which is the useful half.
    const user = userEvent.setup({ delay: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return new Response(
            JSON.stringify({
              error: {
                message: "this pattern cannot be used: invalid escape sequence: \\1",
              },
            }),
            { status: 400, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify(status()), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    renderScreen(<AdminRedaction />);

    await user.click(await screen.findByRole("button", { name: "Add pattern" }));
    await user.type(screen.getByLabelText("Pattern 1 name"), "BAD");
    await user.type(screen.getByLabelText("Pattern 1 regex"), "(a)\\1");
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() =>
      expect(screen.getByText(/invalid escape sequence/)).toBeInTheDocument(),
    );
  });

  it("removes a pattern", async () => {
    const user = userEvent.setup({ delay: null });
    const { fetch: stub, captured } = withEngineChange(status(), status());
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.click(await screen.findByRole("button", { name: "Add pattern" }));
    await user.type(screen.getByLabelText("Pattern 1 name"), "TICKET_ID");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    await waitFor(() => expect(captured.puts).toHaveLength(1));
    const put = captured.puts[0] as { body: Record<string, any> };
    expect(put.body.policy.patterns).toEqual([]);
  });

  it("shows what the provider would receive, and why each span was replaced", async () => {
    // The only place an operator can see what the detector actually does. Until
    // it existed, the Italian bug was findable only by reading an upstream
    // request body.
    const user = userEvent.setup({ delay: null });
    const { fetch: stub, posts } = withPreview(preview());
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.type(await screen.findByLabelText("Sample"), SAMPLE);
    await user.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() => expect(posts).toEqual([{ text: SAMPLE }]));
    expect(
      screen.getByText("<PERSON> le notizie del giorno da ilpost.it"),
    ).toBeInTheDocument();

    const table = screen.getByRole("table");
    // The matched text is sliced from the sample: the API reports offsets, and
    // an offset tells nobody which word was replaced.
    expect(within(table).getByText("Riassumi")).toBeInTheDocument();
    expect(within(table).getByText("ilpost.it")).toBeInTheDocument();
    expect(within(table).getByText("Person name")).toBeInTheDocument();
    expect(within(table).getByText("0.85")).toBeInTheDocument();
    expect(within(table).getByText("Redact")).toBeInTheDocument();
    // Detected, and deliberately left alone. Without this the row reads as a
    // detection that silently did nothing.
    expect(within(table).getByText("Allow-listed")).toBeInTheDocument();
  });

  it("previews as a chosen subject, and waits for one", async () => {
    const user = userEvent.setup({ delay: null });
    const { fetch: stub, posts } = withPreview(preview({ scope: "group", rule_id: "r1" }));
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.type(await screen.findByLabelText("Sample"), SAMPLE);
    await user.selectOptions(screen.getByLabelText("Preview as"), "group");
    // A scope with no subject is the deployment policy wearing a label, which
    // is a different question from the one being asked.
    expect(screen.getByRole("button", { name: "Run preview" })).toBeDisabled();

    await waitFor(() => expect(screen.getByLabelText("Group")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Group"), "g1");
    await user.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() =>
      expect(posts).toEqual([{ text: SAMPLE, scope: "group", scope_id: "g1" }]),
    );
    expect(screen.getByText(/Group rule/)).toBeInTheDocument();
  });

  it("says plainly that a sample would be blocked, and shows no rewrite", async () => {
    // Nothing is rewritten in that case, so echoing the sample back would read
    // as "this is what would be sent".
    const user = userEvent.setup({ delay: null });
    const { fetch: stub } = withPreview(
      preview({
        blocked: true,
        blocked_reason: "a CREDIT_CARD was found, and the policy blocks it",
        redacted_text: null,
        entity_count: 0,
      }),
    );
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.type(await screen.findByLabelText("Sample"), SAMPLE);
    await user.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() =>
      expect(screen.getByText("This request would be blocked")).toBeInTheDocument(),
    );
    expect(screen.getByText(/the policy blocks it/)).toBeInTheDocument();
    expect(screen.queryByText("What the provider receives")).not.toBeInTheDocument();
  });

  it("renders the engine's own note when it detects nothing", async () => {
    const user = userEvent.setup({ delay: null });
    const { fetch: stub } = withPreview(
      preview({
        spans: [],
        entity_count: 0,
        redacted_text: SAMPLE,
        note: "the 'noop' engine detects nothing, so this sample would reach the provider exactly as it is written",
      }),
    );
    vi.stubGlobal("fetch", stub);
    renderScreen(<AdminRedaction />);

    await user.type(await screen.findByLabelText("Sample"), SAMPLE);
    await user.click(screen.getByRole("button", { name: "Run preview" }));

    await waitFor(() => expect(screen.getByText(/detects nothing/)).toBeInTheDocument());
  });

  it("offers the way through to the scoped rules", async () => {
    // Not a nav item: this is the second question about redaction, asked by
    // somebody already looking at the first.
    vi.stubGlobal("fetch", respondWith(status()));
    renderScreen(<AdminRedaction />);

    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Scoped rules" })).toHaveAttribute(
        "href",
        "/admin/redaction/rules",
      ),
    );
  });
});
