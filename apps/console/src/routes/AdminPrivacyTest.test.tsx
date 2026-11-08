import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrivacyProfile } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacyTest } from "./AdminPrivacyTest";

/** The test box, and the profiles page. */

const RESULT = {
  kind: "read",
  outcome: "reader",
  outcome_text: "Read with docling, whatever model was named.",
  fired: { id: "r2", name: "Medical PDFs", action: { type: "reader", reader: "docling", model: null } },
  observed: [{ id: "r9", name: "Trial", action: { type: "refuse", reader: null, model: null } }],
  steps: [
    {
      rule_id: "r1",
      name: "Images",
      position: 0,
      result: "not_matched",
      reasons: ["The file (pdf) is not image."],
      degraded: [],
    },
    { rule_id: "r2", name: "Medical PDFs", position: 1, result: "fired", reasons: [], degraded: [] },
  ],
  facts: { filename: "referto-rossi.pdf", filetype: "pdf", is_scan: null, text_checked: false },
  verdicts: {},
  fallbacks: [],
  degraded: [],
  needs: null,
  extractor: "docling",
  fallback_reason: "docling busy",
};

const PREVIEW = {
  scope: "group",
  rule_id: "rr1",
  engine: "http",
  spans: [
    { entity_type: "PERSON", start: 0, end: 5, score: 0.9, mode: "anonymise", threshold: 0.5, allow_listed: false },
  ],
  redacted_text: "<PERSON> ha una diagnosi",
  entity_count: 1,
  blocked: false,
  blocked_reason: null,
  note: null,
};

function profile(overrides: Partial<PrivacyProfile>): PrivacyProfile {
  return {
    id: "p1",
    name: "Fast",
    description: "",
    engine: "presidio",
    labels: [],
    fallback: "refuse",
    fallback_profile_id: null,
    fallback_profile_name: null,
    is_default: true,
    status: "available",
    status_detail: "The detection service is the one in use for redaction.",
    decides: true,
    cost_hint: "instant",
    rules_using: 2,
    created_at: "2026-11-08T10:00:00Z",
    updated_at: "2026-11-08T10:00:00Z",
    ...overrides,
  } as PrivacyProfile;
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

function stub(calls: Call[], profiles: PrivacyProfile[] = [profile({})], redaction: unknown = PREVIEW) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/groups")) return jsonResponse({ items: [], total: 0, limit: 50, offset: 0 });
      if (url.endsWith("/privacy/test")) return jsonResponse(RESULT);
      if (url.endsWith("/redaction/preview")) return jsonResponse(redaction);
      if (url.includes("/privacy/profiles") && method === "PATCH") return jsonResponse(profiles[0]);
      if (url.includes("/privacy/profiles") && method === "DELETE") return new Response(null, { status: 204 });
      if (url.includes("/privacy/profiles") && method === "POST") return jsonResponse(profiles[0], 201);
      return jsonResponse(profiles);
    }),
  );
}

function renderScreen(element: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the test box", () => {
  it("sends the facts of a file and shows the rule, the trail and what was seen", async () => {
    const user = userEvent.setup();
    const calls: Call[] = [];
    stub(calls);
    renderScreen(<AdminPrivacyTest />);

    await user.type(screen.getByLabelText("File name"), "referto-rossi.pdf");
    await user.type(screen.getByLabelText("File type (MIME)"), "application/pdf");
    await user.selectOptions(screen.getByLabelText("Is it a scan?"), "no");
    await user.type(screen.getByLabelText("Groups"), "Clinic, Staff");
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("Read with docling, whatever model was named.")).toBeInTheDocument();
    expect(calls[0]?.body).toEqual({
      kind: "read",
      groups: ["Clinic", "Staff"],
      filename: "referto-rossi.pdf",
      mime: "application/pdf",
      is_scan: false,
    });
    expect(screen.getByText("Rule that applies: Medical PDFs")).toBeInTheDocument();
    expect(screen.getByText("Also observed: Trial")).toBeInTheDocument();
    expect(screen.getByText("Read with docling. Fell back: docling busy.")).toBeInTheDocument();

    // The trail and the facts are behind disclosures.
    await user.click(screen.getByText("Why this rule"));
    expect(screen.getByText("The file (pdf) is not image.")).toBeInTheDocument();
    await user.click(screen.getByText("What Pystino saw"));
    expect(screen.getByText("referto-rossi.pdf")).toBeInTheDocument();
  });

  it("asks for text and a target when the kind is embedding", async () => {
    const user = userEvent.setup();
    const calls: Call[] = [];
    stub(calls);
    renderScreen(<AdminPrivacyTest />);
    await user.selectOptions(screen.getByLabelText("Kind of request"), "embed");
    await user.type(screen.getByLabelText("Text"), "una diagnosi");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls.find((c) => c.url.endsWith("/privacy/test"))?.body).toEqual({
      kind: "embed",
      groups: [],
      text: "una diagnosi",
      target_local: false,
    });
    // The same text goes to the redaction preview, in the same click.
    expect(calls.find((c) => c.url.endsWith("/redaction/preview"))?.body).toEqual({
      text: "una diagnosi",
    });
  });

  it("shows the four steps in order, with the deciding redaction rule", async () => {
    const user = userEvent.setup();
    const calls: Call[] = [];
    stub(calls);
    renderScreen(<AdminPrivacyTest />);
    await user.type(screen.getByLabelText("File name"), "referto-rossi.pdf");
    await user.type(screen.getByLabelText("Text"), "Mario ha una diagnosi");
    await user.click(screen.getByRole("button", { name: "Run" }));

    const steps = await screen.findAllByRole("listitem");
    const names = steps
      .map((li) => li.textContent ?? "")
      .filter((t) => /^[1-4] (Route|Detect|Hide|Send or refuse)/.test(t))
      .map((t) => t.slice(0, 1))
      .slice(-4);
    expect(names).toEqual(["1", "2", "3", "4"]);
    expect(await screen.findByText("<PERSON> ha una diagnosi")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the deciding redaction rule" })).toHaveAttribute(
      "href",
      "/admin/privacy/hidden/rules/rr1",
    );
    expect(screen.getByText("Detector profile: Fast. Patterns always run.")).toBeInTheDocument();
    expect(screen.getByText("Sent, with what gets hidden removed.")).toBeInTheDocument();
    // Read kind: the text is judged for what gets hidden, never sent to routing.
    expect(calls.find((c) => c.url.endsWith("/privacy/test"))?.body).not.toHaveProperty("text");
  });

  it("says what is missing instead of guessing when there is no text", async () => {
    const user = userEvent.setup();
    const calls: Call[] = [];
    stub(calls);
    renderScreen(<AdminPrivacyTest />);
    await user.type(screen.getByLabelText("File name"), "a.pdf");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Add a text to see what gets hidden.")).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/redaction/preview"))).toBe(false);
  });

  it("shows why each span was replaced, and the allow-listed ones left alone", async () => {
    const user = userEvent.setup();
    stub([], [profile({})], {
      ...PREVIEW,
      spans: [
        ...PREVIEW.spans,
        { entity_type: "URL", start: 10, end: 19, score: 0.6, mode: "off", threshold: 0.5, allow_listed: true },
      ],
    });
    renderScreen(<AdminPrivacyTest />);
    await user.type(screen.getByLabelText("File name"), "a.pdf");
    await user.type(screen.getByLabelText("Text"), "Mario ha una diagnosi");
    await user.click(screen.getByRole("button", { name: "Run" }));
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Mario")).toBeInTheDocument();
    expect(within(table).getByText("Person name")).toBeInTheDocument();
    expect(within(table).getByText("Allow-listed")).toBeInTheDocument();
  });

  it("says plainly that a sample would be blocked, and ends in a refusal", async () => {
    const user = userEvent.setup();
    stub([], [profile({})], {
      ...PREVIEW,
      spans: [],
      entity_count: 0,
      redacted_text: null,
      blocked: true,
      blocked_reason: "a CREDIT_CARD was found, and the policy blocks it",
    });
    renderScreen(<AdminPrivacyTest />);
    await user.type(screen.getByLabelText("File name"), "a.pdf");
    await user.type(screen.getByLabelText("Text"), "4111 1111 1111 1111");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("This request would be blocked")).toBeInTheDocument();
    expect(screen.getByText("Refused: nothing is sent.")).toBeInTheDocument();
  });
});
