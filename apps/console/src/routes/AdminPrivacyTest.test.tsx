import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PrivacyProfile } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { AdminPrivacyProfiles } from "./AdminPrivacyProfiles";
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

function stub(calls: Call[], profiles: PrivacyProfile[] = [profile({})]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.endsWith("/privacy/test")) return jsonResponse(RESULT);
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
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.body).toEqual({
      kind: "embed",
      groups: [],
      text: "una diagnosi",
      target_local: false,
    });
  });
});

describe("the profiles page", () => {
  it("shows health and the fallback, and the default cannot be deleted", async () => {
    stub([], [
      profile({}),
      profile({
        id: "p2",
        name: "Medical",
        engine: "gliner",
        is_default: false,
        status: "not_installed",
        status_detail: "This engine is not installed here.",
        decides: false,
        fallback: "fallback_to",
        fallback_profile_id: "p1",
        fallback_profile_name: "Fast",
        rules_using: 0,
      }),
    ]);
    renderScreen(<AdminPrivacyProfiles />);

    const fast = (await screen.findByRole("heading", { name: /^Fast/ })).closest("section")!;
    expect(within(fast).getByText("Available")).toBeInTheDocument();
    expect(within(fast).getByText("Default")).toBeInTheDocument();
    expect(within(fast).queryByRole("button", { name: "Delete profile" })).toBeNull();

    const medical = screen.getByRole("heading", { name: /^Medical/ }).closest("section")!;
    expect(within(medical).getByText("Not installed")).toBeInTheDocument();
    expect(within(medical).getByText(/protection is weaker, and the log says so/)).toBeInTheDocument();
    expect(within(medical).getByRole("button", { name: "Delete profile" })).toBeInTheDocument();
  });

  it("changes a profile's fallback", async () => {
    const user = userEvent.setup();
    const calls: Call[] = [];
    stub(calls, [profile({}), profile({ id: "p2", name: "Medical", is_default: false })]);
    renderScreen(<AdminPrivacyProfiles />);
    const medical = (await screen.findByRole("heading", { name: /^Medical/ })).closest("section")!;
    await user.selectOptions(within(medical).getByLabelText("If it is unavailable"), "fallback_to");
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({
      method: "PATCH",
      body: { fallback: "fallback_to", fallback_profile_id: "p1" },
    });
  });
});
