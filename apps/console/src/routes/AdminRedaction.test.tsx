import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedactionStatus } from "../lib/types";
import { AdminRedaction } from "./AdminRedaction";

/**
 * The screen exists to answer "is redaction on, and is it working". So what is
 * pinned is the difference between those two — a layer that is switched on and
 * detecting nothing looks identical to one with nothing to find, and only the
 * entity count and the API's warnings tell them apart.
 */

function status(overrides: Partial<RedactionStatus> = {}): RedactionStatus {
  return {
    engine: "http",
    enabled: true,
    endpoint: "http://redaction:8080",
    installed_engines: ["noop", "http"],
    fail_open: false,
    restore_in_response: true,
    language: "en",
    score_threshold: 0.5,
    entity_types: null,
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

  it("distinguishes 'every entity type' from 'none'", async () => {
    // Null and empty are different facts. Conflating them is the difference
    // between redacting everything and redacting nothing.
    vi.stubGlobal("fetch", respondWith(status({ entity_types: null })));
    const { unmount } = renderScreen(<AdminRedaction />);
    await waitFor(() =>
      expect(screen.getByText(/Every type the engine offers/)).toBeInTheDocument(),
    );
    // Falls back to the service's list so the reader can see what that means.
    expect(screen.getByText("PERSON")).toBeInTheDocument();
    unmount();

    vi.stubGlobal("fetch", respondWith(status({ entity_types: ["PERSON"] })));
    renderScreen(<AdminRedaction />);
    await waitFor(() => expect(screen.getByText(/Only these are looked for/)).toBeInTheDocument());
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
});
