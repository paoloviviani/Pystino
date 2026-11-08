import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedactionEvent } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { MyRedactions } from "./MyRedactions";

/** The member's Redactions page: the log of requests where something was hidden or a
 *  pipeline refused, with no values in it, and a pointer to their own pipelines (ADR 0113). */

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
    routing: null,
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

function api(seen: Seen[], options: { events?: RedactionEvent[] } = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    seen.push({
      method: init?.method ?? "GET",
      url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
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
  it("points at the user's own privacy pipelines", async () => {
    vi.stubGlobal("fetch", api([]));
    renderPage();
    const link = await screen.findByRole("link", { name: "Privacy pipelines" });
    expect(link).toHaveAttribute("href", "/privacy");
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

  it("shows what Pystino saw and what handled the request, compactly", async () => {
    vi.stubGlobal(
      "fetch",
      api([], {
        events: [
          event({
            entities: [],
            routing: {
              v: 2,
              kind: "read",
              outcome: "rule",
              observed: {
                target: { alias: null, model: "local-reader", provider: "local" },
                filename: "referto-rossi.pdf",
                mime: "application/pdf",
                filetype: "pdf",
                is_scan: true,
                verdicts: null,
              },
              handled: {
                extractor: "docling",
                fallback_reason: "docling busy",
                reader: "docling",
                profile: "default",
                routing_rule: {
                  id: "r1",
                  name: "Medical PDFs",
                  pipeline: "Reports",
                  pipeline_id: "p1",
                  owner: "admin",
                },
                engine: null,
                policies: [],
              },
              pipelines: [],
              observed_rules: [],
              degraded: [],
              fallbacks: [],
            } as never,
          }),
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText("Reading a file · Rule Reports › Medical PDFs")).toBeInTheDocument();
    expect(screen.getByText("Read with docling")).toBeInTheDocument();
    expect(screen.getByText("pdf · scan")).toBeInTheDocument();
    // A fallback is a warning, not a footnote.
    expect(screen.getByText("Fell back: docling busy")).toBeInTheDocument();
    // A request where nothing was hidden is still in the log, and says so.
    expect(screen.getByText("Nothing hidden")).toBeInTheDocument();
    expect(screen.getByText("Checked")).toBeInTheDocument();
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
