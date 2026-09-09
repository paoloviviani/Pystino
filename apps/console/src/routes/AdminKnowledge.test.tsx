import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeBaseSummary, KnowledgeStatus } from "../lib/types";
import { AdminKnowledge } from "./AdminKnowledge";

/**
 * The screen exists to answer one question an administrator cannot answer any
 * other way: *which bases would a reindex change*.
 *
 * So what is pinned is the relationship between the setting and its
 * consequences — that changing the embedding model leaves existing bases alone
 * and says so, that a base on an older model is marked rather than silently
 * broken, and that the one change which sends documents to a third party is the
 * one that asks why.
 */

function base(overrides: Partial<KnowledgeBaseSummary> = {}): KnowledgeBaseSummary {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    name: "Handbook",
    description: "",
    owner_email: "owner@example.org",
    group_name: "research",
    embedding_model: "bge-m3",
    dimensions: 1024,
    document_count: 3,
    chunk_count: 42,
    failed_count: 0,
    stale: false,
    share_count: 0,
    created_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

function status(overrides: Partial<KnowledgeStatus> = {}): KnowledgeStatus {
  return {
    enabled: true,
    ready: true,
    embedding_model: "bge-m3",
    extractor_model: null,
    vector_store: "pgvector",
    chunk_chars: 1200,
    chunk_overlap: 150,
    source: "console",
    propagation_seconds: 10,
    detail: null,
    available_embedding_models: ["bge-m3", "text-embedding-3-small"],
    available_extractor_models: ["mistral-ocr"],
    bases: [base()],
    stale_base_count: 0,
    history: [],
    ...overrides,
  };
}

function respondWith(body: KnowledgeStatus) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
}

interface Captured {
  writes: { url: string; method: string; body: unknown }[];
}

/**
 * A fetch stub that records writes and answers them with *after*.
 *
 * The routes return the whole status document so the screen updates from the
 * response rather than refetching. Modelling that is the point: a stub that
 * answered writes with the *old* document would let a test pass against a
 * screen that quietly showed stale state.
 */
function withWrites(before: KnowledgeStatus, after: KnowledgeStatus): Captured {
  const captured: Captured = { writes: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method !== "GET") {
        captured.writes.push({
          url,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        return new Response(JSON.stringify(after), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(before), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return captured;
}

function mount(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AdminKnowledge", () => {
  it("says nothing can be indexed when no embedding model is chosen", async () => {
    // Enabled but unconfigured is *unfinished*, not broken, and the screen has
    // to say which decision is missing — otherwise the only symptom is uploads
    // failing for users.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          ready: false,
          embedding_model: null,
          detail: "No embedding model has been chosen, so nothing can be indexed yet.",
        }),
      ),
    );
    mount(<AdminKnowledge />);
    expect(await screen.findByText(/no embedding model has been chosen/i)).toBeInTheDocument();
  });

  it("distinguishes 'none chosen' from 'none installed'", async () => {
    // Two different instructions: pick one, versus go and create one first.
    // Offering an empty dropdown for the second is how an admin screen wastes
    // somebody's afternoon.
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          ready: false,
          embedding_model: null,
          available_embedding_models: [],
          detail:
            "This deployment has no embedding model. Import or create one first, then choose it here.",
        }),
      ),
    );
    mount(<AdminKnowledge />);
    // Said in two places on purpose — the banner and the field's own hint — so
    // this asserts "at least once" rather than pinning which one.
    expect((await screen.findAllByText(/import or create one/i)).length).toBeGreaterThan(0);
    // And the control is unavailable rather than an empty dropdown that looks
    // like a choice.
    await waitFor(() => expect(screen.getByLabelText(/embedding model/i)).toBeDisabled());
  });

  it("sends only the fields that changed", async () => {
    // The server's rule: a null column means "this row does not decide", so
    // sending a whole document would overwrite settings nobody touched — and
    // silently re-chunk every base created afterwards.
    const before = status();
    const captured = withWrites(before, status({ embedding_model: "text-embedding-3-small" }));
    mount(<AdminKnowledge />);

    const select = await screen.findByLabelText(/embedding model/i);
    await userEvent.selectOptions(select, "text-embedding-3-small");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(captured.writes).toHaveLength(1));
    const write = captured.writes[0];
    expect(write?.method).toBe("PUT");
    expect(write?.body).toEqual({ embedding_model: "text-embedding-3-small" });
  });

  it("asks why before sending documents to a third-party extractor", async () => {
    // The only change here that alters *where user documents go*. The server
    // enforces it too; the form states it so the refusal is never a surprise.
    withWrites(status(), status({ extractor_model: "mistral-ocr" }));
    mount(<AdminKnowledge />);

    const extractor = await screen.findByLabelText(/document extraction/i);
    expect(screen.queryByLabelText(/^why$/i)).not.toBeInTheDocument();
    await userEvent.selectOptions(extractor, "mistral-ocr");
    expect(await screen.findByLabelText(/^why$/i)).toBeInTheDocument();
  });

  it("does not ask why for returning to the built-in extractor", async () => {
    // Coming back to the extractor that never sends a document anywhere is a
    // change that protects *more*, and demanding a sentence for it is how
    // people learn to type "x".
    withWrites(status({ extractor_model: "mistral-ocr" }), status({ extractor_model: null }));
    mount(<AdminKnowledge />);

    const extractor = await screen.findByLabelText(/document extraction/i);
    await userEvent.selectOptions(extractor, "");
    expect(screen.queryByLabelText(/^why$/i)).not.toBeInTheDocument();
  });

  it("marks a base indexed with an older model, and does not call it broken", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          embedding_model: "text-embedding-3-small",
          bases: [base({ embedding_model: "bge-m3", stale: true })],
          stale_base_count: 1,
        }),
      ),
    );
    mount(<AdminKnowledge />);

    expect(await screen.findByText(/older model/i)).toBeInTheDocument();
    // The wording matters: these bases still answer searches from their own
    // vectors, which is the whole reason the model is pinned per base.
    expect(screen.getByText(/still answer searches/i)).toBeInTheDocument();
  });

  it("reindexes one base and reports it", async () => {
    const captured = withWrites(
      status({ bases: [base({ stale: true })], stale_base_count: 1 }),
      status({ bases: [base()], stale_base_count: 0 }),
    );
    mount(<AdminKnowledge />);

    await userEvent.click(await screen.findByRole("button", { name: /reindex/i }));
    await waitFor(() => expect(captured.writes).toHaveLength(1));
    const write = captured.writes[0];
    expect(write?.method).toBe("POST");
    expect(write?.url).toContain(
      "/api/admin/knowledge/bases/11111111-1111-4111-8111-111111111111/reindex",
    );
  });

  it("cannot reindex while no embedding model is configured", async () => {
    // The button would only produce a 400, and a control that always fails is
    // worse than one that is visibly unavailable.
    vi.stubGlobal(
      "fetch",
      respondWith(status({ ready: false, embedding_model: null, detail: "…" })),
    );
    mount(<AdminKnowledge />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /reindex/i })).toBeDisabled(),
    );
  });

  it("shows a document failure count where there is one", async () => {
    vi.stubGlobal("fetch", respondWith(status({ bases: [base({ failed_count: 2 })] })));
    mount(<AdminKnowledge />);
    expect(await screen.findByText(/2 failed/i)).toBeInTheDocument();
  });

  it("says where the configuration comes from and how long it takes to land", async () => {
    // The two disagreeing is invisible otherwise, and that confusion is
    // exactly what a database override introduces.
    vi.stubGlobal("fetch", respondWith(status({ source: "environment" })));
    mount(<AdminKnowledge />);
    expect(await screen.findByText(/decided in the environment/i)).toBeInTheDocument();
    expect(screen.getByText(/within 10s/i)).toBeInTheDocument();
  });

  it("lists past changes with who made them and why", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          history: [
            {
              id: "22222222-2222-4222-8222-222222222222",
              embedding_model: "bge-m3",
              extractor_model: "mistral-ocr",
              vector_store: "pgvector",
              chunk_chars: 1200,
              chunk_overlap: 150,
              reason: "Scanned invoices need OCR.",
              changed_by: "admin@example.org",
              created_at: "2026-09-02T09:00:00Z",
            },
          ],
        }),
      ),
    );
    mount(<AdminKnowledge />);
    const changes = await screen.findByText(/scanned invoices need ocr/i);
    expect(changes).toBeInTheDocument();
    expect(screen.getByText("admin@example.org")).toBeInTheDocument();
  });

  it("says the feature is off rather than showing an empty screen", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith(
        status({
          enabled: false,
          ready: false,
          bases: [],
          detail: "Knowledge bases are switched off for this deployment.",
        }),
      ),
    );
    mount(<AdminKnowledge />);
    expect(await screen.findByText(/switched off for this deployment/i)).toBeInTheDocument();
  });

  it("says bases are created by users, not here", async () => {
    // The empty state has to prevent the obvious wrong conclusion, which is
    // that an administrator is supposed to create them.
    vi.stubGlobal("fetch", respondWith(status({ bases: [], stale_base_count: 0 })));
    mount(<AdminKnowledge />);
    const table = await screen.findByText(/created by users, not here/i);
    expect(table).toBeInTheDocument();
  });

  it("keeps an owner-erased base listed", async () => {
    // A base whose owner was erased under GDPR still exists and still costs
    // storage; hiding it would make it unreachable from the only screen that
    // could deal with it.
    vi.stubGlobal("fetch", respondWith(status({ bases: [base({ owner_email: null })] })));
    mount(<AdminKnowledge />);
    const row = await screen.findByText("Handbook");
    expect(within(row.closest("tr") as HTMLElement).getByText(/erased/i)).toBeInTheDocument();
  });
});
