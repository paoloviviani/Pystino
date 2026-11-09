import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  RedactionEngineOption,
  RedactionStatus,
} from "../lib/types";
import { AdminPrivacyService } from "./AdminPrivacyService";

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
    presidio_pattern_matching: null,
    presidio_ner: null,
    propagation_seconds: 10,
    fail_open: false,
    restore_in_response: true,
    restore_in_tool_arguments: true,
    language: "en",
    score_threshold: 0.5,
    entity_types: null,
    policy: {
      default_mode: "anonymise_restore",
      engine: null,
      formats: {},
      entities: { URL: { mode: "off", threshold: null } },
      custom_entities: [],
      patterns: [],
      keywords: [],
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
      pattern_entities: ["EMAIL_ADDRESS"],
      model_entities: ["PERSON"],
      family_partition: true,
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

/**
 * Where the router went. MemoryRouter keeps its location to itself and nothing
 * on screen spells the path out, so a component that reports it is the only way
 * to tell "the control navigated" from "the control did nothing".
 */
const location = { pathname: "/" };

function Probe() {
  location.pathname = useLocation().pathname;
  return null;
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
function renderScreen(element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        {element}
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

/** The Redaction service tab: the engine choice and the detection families, with no profile cards. */
function renderDetectors() {
  return renderScreen(<AdminPrivacyService />);
}

describe("Redaction service (the former Redaction screen)", () => {
  it("says whether redaction is on, and which engine", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

    // Scoped to the stat block: the detector-family switches elsewhere on
    // the page now also render visible On/Off text.
    await waitFor(() => expect(screen.getByText("http engine")).toBeInTheDocument());
    const stat = screen.getByText("http engine").closest("div.flex") as HTMLElement;
    expect(within(stat).getByText("On")).toBeInTheDocument();
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
    renderDetectors();

    await waitFor(() =>
      expect(screen.getByText("prompts reach the provider unchanged")).toBeInTheDocument(),
    );
    const stat = screen
      .getByText("prompts reach the provider unchanged")
      .closest("div.flex") as HTMLElement;
    expect(within(stat).getByText("Off")).toBeInTheDocument();
    expect(screen.getByText(/is not enabled/)).toBeInTheDocument();
  });

  it("lists every installed engine with what it does, not just its name", async () => {
    // A name is not a choice: "http" and "noop" say nothing about which one
    // leaves personal data in a prompt. The description is what is being chosen.
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

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
    renderDetectors();

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
    renderDetectors();

    await user.click(await screen.findByRole("button", { name: "Enable" }));

    await waitFor(() => expect(captured.puts.length).toBe(1));
    expect(captured.puts[0]!.url).toContain("/api/admin/redaction/engine");
    expect(captured.puts[0]!.body).toEqual({ engine: "http", reason: "" });
  });

  it.each(["noop", "regex"])(
    "dims the detection families and disables the switches when %s is in force",
    async (name) => {
      vi.stubGlobal("fetch", respondWith(status({ engine: name, enabled: name !== "noop" })));
      renderDetectors();

      const patterns = await screen.findByRole("switch", { name: "Pattern matching" });
      const ner = screen.getByRole("switch", { name: "Named-entity recognition" });
      for (const sw of [patterns, ner]) {
        expect(sw).toBeDisabled();
        expect(sw).toHaveAttribute("aria-disabled", "true");
        // Stored values stay shown.
        expect(sw).toHaveAttribute("aria-checked", "true");
      }
      expect(
        screen.getByText("Used only when Presidio is the engine in force. Enable it above."),
      ).toBeInTheDocument();
      expect(screen.getAllByText("(Presidio only)").length).toBeGreaterThan(0);
    },
  );

  it("keeps the detection families live when Presidio is in force", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

    const patterns = await screen.findByRole("switch", { name: "Pattern matching" });
    expect(patterns).toBeEnabled();
    expect(patterns).not.toHaveAttribute("aria-disabled");
    expect(screen.queryByText(/Used only when Presidio is the engine in force/)).toBeNull();
    expect(screen.queryByText("(Presidio only)")).toBeNull();
  });

  it("saves each detector family with a big switch, immediately", async () => {
    // The detector families are independent console decisions, not a rebuild:
    // flipping a switch writes the row at once, with no separate save step and
    // no third "default" state to pick.
    const before = status({ presidio_pattern_matching: null, presidio_ner: null });
    const after = status({ presidio_pattern_matching: true, presidio_ner: false });
    const { fetch, captured } = withEngineChange(before, after);
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup({ delay: null });
    renderDetectors();

    const patterns = await screen.findByRole("switch", { name: "Pattern matching" });
    const entities = screen.getByRole("switch", { name: "Named-entity recognition" });
    expect(patterns).toHaveAttribute("aria-checked", "true");
    expect(entities).toHaveAttribute("aria-checked", "true");

    await user.click(entities);

    await waitFor(() => expect(captured.puts).toHaveLength(1));
    expect(captured.puts[0]!.body).toEqual({
      engine: "http",
      reason: "",
      presidio_pattern_matching: true,
      presidio_ner: false,
    });
    // The response becomes the new starting position.
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Named-entity recognition" }),
      ).toHaveAttribute("aria-checked", "false"),
    );
  });

  it("starts the switches from the deployment default", async () => {
    // No console choice saved yet, and the detector build offers no NER model:
    // the model switch starts off rather than pretending names are detected.
    // The pattern switch starts on, because patterns are installed.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          presidio_pattern_matching: null,
          presidio_ner: null,
          service: {
            ...status().service!,
            entities: ["EMAIL_ADDRESS"],
            pattern_entities: ["EMAIL_ADDRESS"],
            model_entities: [],
          },
        }),
      ),
    );
    renderDetectors();

    expect(
      await screen.findByRole("switch", { name: "Pattern matching" }),
    ).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByRole("switch", { name: "Named-entity recognition" }),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("reverts a switch when the save fails", async () => {
    // An optimistic flip that the API refuses must not sit showing a state
    // that is not in force.
    const stub = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return new Response(JSON.stringify({ error: { message: "nope" } }), {
          status: 400,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(status()), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", stub as unknown as typeof fetch);
    const user = userEvent.setup({ delay: null });
    renderDetectors();

    const entities = await screen.findByRole("switch", { name: "Named-entity recognition" });
    await user.click(entities);

    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Named-entity recognition" }),
      ).toHaveAttribute("aria-checked", "true"),
    );
  });

  it("stops and asks before turning redaction off, and asks for nothing", async () => {
    // Still the one change that makes the system quietly stop protecting
    // anything, so it still confirms. It no longer demands a written reason:
    // that was dropped deliberately, on the grounds that a sentence typed to
    // get past a dialog is not an audit trail, and the row records the engine,
    // who chose it and when regardless.
    const { fetch, captured } = withEngineChange(
      status(),
      status({ engine: "noop", enabled: false }),
    );
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    renderDetectors();

    await user.click(await screen.findByRole("button", { name: "Turn redaction off" }));

    const dialog = within(await screen.findByRole("dialog"));
    // What the change does is still said, in red, before anything is sent.
    expect(
      dialog.getByText(/prompts will reach providers exactly as callers sent them/i),
    ).toBeInTheDocument();
    expect(dialog.queryByLabelText("Reason")).not.toBeInTheDocument();
    expect(captured.puts.length).toBe(0);

    await user.click(dialog.getByRole("button", { name: "Turn it off" }));

    await waitFor(() => expect(captured.puts.length).toBe(1));
    expect(captured.puts[0]!.body).toEqual({ engine: "noop", reason: "" });
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
            presidio_ner: null,
            presidio_pattern_matching: null,
          },
        }),
      ),
    );
    renderDetectors();

    await waitFor(() => expect(screen.getByText(/This console/)).toBeInTheDocument());
    expect(screen.getByText("turned back on after the migration")).toBeInTheDocument();
    expect(screen.getByText(/dave@example\.org/)).toBeInTheDocument();
  });

  it("names the environment variable when nothing has been set here", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

    await waitFor(() =>
      expect(screen.getByText(/GATEWAY_REDACTION__ENGINE/)).toBeInTheDocument(),
    );
  });

  it("reports the detection service as it answered just now", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

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
    renderDetectors();

    await waitFor(() => expect(screen.getByText("Not answering")).toBeInTheDocument());
    expect(screen.getByText(/connection refused/)).toBeInTheDocument();
  });

  it("shows what redaction has actually removed, not just its settings", async () => {
    // Configuration is not evidence. A wrong language or too high a threshold
    // both look like healthy silence.
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

    await waitFor(() => expect(screen.getByText("31")).toBeInTheDocument());
    expect(screen.getByText(/from 12 of 40 requests, last 24 hours/)).toBeInTheDocument();
  });

  it("renders the API's warnings verbatim", async () => {
    const note = "fail_open is on, so a detection failure forwards the prompt unredacted.";
    vi.stubGlobal("fetch", respondWith(status({ fail_open: true, warnings: [note] })));
    renderDetectors();

    await waitFor(() => expect(screen.getByText(note)).toBeInTheDocument());
    expect(screen.getByText("Forward unredacted")).toBeInTheDocument();
  });

  it("names the model backing the configured language", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

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
    renderDetectors();

    await waitFor(() =>
      expect(screen.getByText(/Served without a named-entity model/)).toBeInTheDocument(),
    );
    const card = screen.getByText("Degraded languages").closest("section")!;
    expect(within(card).getByText("it")).toBeInTheDocument();
  });

  it("never renders the placeholder key, only whether it is set", async () => {
    vi.stubGlobal("fetch", respondWith(status()));
    renderDetectors();

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
    renderDetectors();

    await waitFor(() => expect(screen.getByText("Admin access required.")).toBeInTheDocument());
  });
});

describe("Redaction service: what is left of the detector profiles", () => {
  function stubService(calls: { method: string; url: string; body: unknown }[] = []) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
        if (url.includes("/privacy/settings")) return json({ user_entity_types: ["PERSON"] });
        return json(status());
      }),
    );
  }

  it("hosts the engine choice and the families directly, with no profile cards", async () => {
    const calls: { method: string; url: string; body: unknown }[] = [];
    stubService(calls);
    renderScreen(<AdminPrivacyService />);

    expect(await screen.findByText("Presidio (detection service)")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Pattern matching" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Named-entity recognition" })).toBeInTheDocument();
    expect(screen.queryByText("Add a profile")).toBeNull();
    expect(screen.queryByText(/profile/i)).toBeNull();
    // Profiles are gone from the API, so nothing asks for them.
    expect(calls.some((c) => c.url.includes("/privacy/profiles"))).toBe(false);
  });

  it("keeps the setting for what users may add to their own policies", async () => {
    const user = userEvent.setup({ delay: null });
    const calls: { method: string; url: string; body: unknown }[] = [];
    stubService(calls);
    renderScreen(<AdminPrivacyService />);

    const field = await screen.findByLabelText("Entity types users may name");
    await waitFor(() => expect(field).toHaveValue("PERSON"));
    await user.type(field, ", URL");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ user_entity_types: ["PERSON", "URL"] }),
    );
  });
});
