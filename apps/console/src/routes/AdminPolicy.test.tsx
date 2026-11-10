import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPolicy } from "./AdminPolicy";

afterEach(() => vi.unstubAllGlobals());

const ENGINES = {
  reachable: true,
  detail: "",
  pattern_entities: ["EMAIL_ADDRESS"],
  engines: [
    {
      name: "spacy",
      available: true,
      default: true,
      entities: ["PERSON"],
      custom_labels: false,
      labels: [],
      ms_per_token: 0.1,
      languages: ["en"],
    },
    {
      name: "gliner",
      available: false,
      default: false,
      entities: ["PERSON"],
      custom_labels: true,
      labels: [],
      ms_per_token: 5,
      languages: ["en"],
    },
  ],
};

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

function renderPage(seen: Seen[] = [], tryAnswer: unknown = null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      seen.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/privacy/engines")) return jsonResponse(ENGINES);
      if (url.endsWith("/policies/try")) return jsonResponse(tryAnswer);
      if (url.includes("/policies") && method === "GET") {
        return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
      }
      return jsonResponse({ id: "new" });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/admin/privacy/policies/new"]}>
        <Routes>
          <Route path="/admin/privacy/policies/:policyId" element={<AdminPolicy />} />
          <Route path="/admin/privacy/policies" element={<p>list</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("A redaction policy page", () => {
  it("has no detector-profile picker: the engine is chosen in the entities section", async () => {
    renderPage();
    expect(await screen.findByLabelText("Engine")).toBeInTheDocument();
    expect(screen.queryByLabelText("Detector profile")).toBeNull();
  });

  it("saves the engine and the shared flag with the policy", async () => {
    const seen: Seen[] = [];
    renderPage(seen);
    const user = userEvent.setup({ delay: null });
    await user.type(await screen.findByLabelText("Name"), "Names");
    await user.click(screen.getByRole("checkbox", { name: /Shared with users/ }));
    await user.click(screen.getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(seen.some((c) => c.method === "POST")).toBe(true));
    expect(seen.find((c) => c.method === "POST")?.body).toMatchObject({
      name: "Names",
      shared_with_users: true,
      policy: { engine: null, formats: {}, keywords: [], custom_entities: [] },
    });
  });

  describe("try it", () => {
    it("sends the policy as currently edited, unsaved, and lists the items by group", async () => {
      const seen: Seen[] = [];
      renderPage(seen, {
        items: [
          { group: "pattern", type: "EMAIL_ADDRESS", engine: null, score: null, mode: "redact", text: "ana@example.org" },
          { group: "entity", type: "PERSON", engine: "spacy", score: 0.91, mode: "detect_only", text: "Ana" },
        ],
        unavailable: null,
        redacted_text: "Write to <EMAIL_ADDRESS>, Ana",
        blocked: false,
      });
      const user = userEvent.setup({ delay: null });
      await user.click(await screen.findByRole("button", { name: "Add keyword list" }));
      await user.type(screen.getByLabelText("Keyword list 1 words"), "alpha, beta");
      await user.type(screen.getByLabelText("Sample text"), "Write to ana@example.org, Ana");
      await user.click(screen.getByRole("button", { name: "Run" }));

      const result = await screen.findByTestId("policy-try-result");
      const sent = seen.find((c) => c.url.endsWith("/policies/try"));
      expect(sent?.method).toBe("POST");
      expect(sent?.body).toMatchObject({
        text: "Write to ana@example.org, Ana",
        policy: { keywords: [{ words: ["alpha", "beta"] }] },
      });
      // Nothing was saved to run it.
      expect(seen.some((c) => c.method === "POST" && !c.url.endsWith("/try"))).toBe(false);

      const patterns = within(result).getByRole("region", { name: "Patterns" });
      expect(within(patterns).getByText("ana@example.org")).toBeInTheDocument();
      const entities = within(result).getByRole("region", { name: "Entities" });
      expect(within(entities).getByText("Ana")).toBeInTheDocument();
      expect(within(entities).queryByText("Detect only")).toBeNull();
      expect(within(entities).getByText("spacy · 0.91")).toBeInTheDocument();
      expect(screen.getByTestId("policy-try-output")).toHaveTextContent("Write to <EMAIL_ADDRESS>, Ana");
    });

    it("says the engine could not run, and does not call that 'nothing found'", async () => {
      renderPage([], {
        items: [],
        unavailable: "the spacy engine did not answer",
        redacted_text: "text",
        blocked: false,
      });
      const user = userEvent.setup({ delay: null });
      await user.type(await screen.findByLabelText("Sample text"), "text");
      await user.click(screen.getByRole("button", { name: "Run" }));
      expect(await screen.findByText("The engine could not run")).toBeInTheDocument();
      expect(screen.getByText(/the spacy engine did not answer/)).toBeInTheDocument();
      expect(screen.queryByText("Nothing found.")).toBeNull();
    });

    it("says nothing was found when the engine ran and found nothing", async () => {
      renderPage([], { items: [], unavailable: null, redacted_text: "text", blocked: false });
      const user = userEvent.setup({ delay: null });
      await user.type(await screen.findByLabelText("Sample text"), "text");
      await user.click(screen.getByRole("button", { name: "Run" }));
      expect(await screen.findByText("Nothing found.")).toBeInTheDocument();
    });

    it("shows a block", async () => {
      renderPage([], { items: [], unavailable: null, redacted_text: null, blocked: true });
      const user = userEvent.setup({ delay: null });
      await user.type(await screen.findByLabelText("Sample text"), "text");
      await user.click(screen.getByRole("button", { name: "Run" }));
      expect(await screen.findByText("Blocked")).toBeInTheDocument();
      expect(screen.queryByTestId("policy-try-output")).toBeNull();
    });
  });
});
