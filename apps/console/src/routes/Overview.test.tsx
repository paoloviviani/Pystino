import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiKey, Me, MyRedaction, RedactionPolicy, UsageReport } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { Overview } from "./Overview";

/**
 * What this screen must get right is not layout, it is honesty about money:
 * amounts come from the API as strings and are never recomputed here, and the
 * API's caveats are shown rather than quietly dropped.
 */

const ME: Me = {
  id: "u1",
  email: "alice@example.org",
  display_name: "Alice",
  is_admin: false,
  groups: [{ id: "g1", name: "research", description: null }],
  default_billing_group: { id: "g1", name: "research", description: null },
};

function report(overrides: Partial<UsageReport> = {}): UsageReport {
  return {
    period: {
      label: "2026-08",
      kind: "month",
      start: "2026-07-31T22:00:00Z",
      end: "2026-08-31T22:00:00Z",
      timezone: "Europe/Rome",
    },
    group_by: "model",
    currency: "EUR",
    rows: [
      {
        key: "gpt-ish",
        label: "gpt-ish",
        requests: 12,
        prompt_tokens: 1000,
        completion_tokens: 500,
        total_tokens: 1500,
        images: 0,
        cost: "3.250000000000",
        estimated_requests: 0,
        unavailable_requests: 0,
      },
    ],
    totals: {
      key: null,
      label: "total",
      requests: 12,
      prompt_tokens: 1000,
      completion_tokens: 500,
      total_tokens: 1500,
      images: 0,
      cost: "3.250000000000",
      estimated_requests: 0,
      unavailable_requests: 0,
    },
    disclosures: [],
    ...overrides,
  };
}


/**
 * What the redaction card reads.
 *
 * `baseline` is what the administrators require and is the document the form is
 * seeded from — a personal policy that omits a type falls back to its own
 * default, so an empty one is a weakening of everything.
 */
function redaction(overrides: Partial<MyRedaction> = {}): MyRedaction {
  const policy: RedactionPolicy = {
    default_mode: "anonymise_restore",
    entities: { PERSON: { mode: "anonymise", threshold: null } },
    patterns: [],
    allow_list: ["ilpost.it"],
  };
  return {
    policy: null,
    rule_id: null,
    updated_at: null,
    effective: policy,
    baseline: policy,
    propagation_seconds: 10,
    ...overrides,
  };
}

function respondWith(body: UsageReport, keys: unknown[] = []) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/me/redaction")) return jsonResponse(redaction());
    const payload = url.includes("/reports/usage") ? body : keys;
    return jsonResponse(payload);
  });
}

function apiKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "k1",
    name: "laptop",
    prefix: "sk-live-abcd",
    billing_group: { id: "g1", name: "research", description: null },
    created_at: "2026-08-01T10:00:00Z",
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    ...overrides,
  };
}

interface Calls {
  minted: unknown[];
  revoked: string[];
}

/**
 * A directory of keys that can be minted from and revoked, so the tests assert
 * on what the server was actually asked for rather than only on what rendered.
 *
 * The secret is supplied by the fake for the same reason the real API supplies
 * it exactly once: the client must never be able to derive or re-request it.
 */
function keyRoutes(existing: ApiKey[], calls: Calls = { minted: [], revoked: [] }) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url.includes("/reports/usage")) return jsonResponse(report());
    if (url.includes("/api/me/redaction")) return jsonResponse(redaction());

    if (url.includes("/api/me/keys")) {
      if (method === "POST") {
        const body = JSON.parse(String(init?.body));
        calls.minted.push(body);
        return jsonResponse(
          { ...apiKey({ id: "new", name: body.name }), secret: "sk-live-THE-ONLY-COPY" },
          201,
        );
      }
      if (method === "DELETE") {
        calls.revoked.push(url.split("/").pop() ?? "");
        return jsonResponse(apiKey({ revoked_at: "2026-08-16T10:00:00Z" }));
      }
      return jsonResponse({ items: existing, total: existing.length, limit: 200, offset: 0 });
    }
    return jsonResponse([]);
  });
}

function renderScreen(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // Pinned so the period picker's default is deterministic; without it the
  // suite would exercise a different month every month.
  vi.setSystemTime(new Date(2026, 7, 15));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Overview", () => {
  it("shows the total the API reported, formatted but not recomputed", async () => {
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen(<Overview me={ME} />);

    // At most three decimals: this screen is for a person reading what they
    // spent, and the ledger's twelve places are noise here. "At most", so a
    // round amount is written the way money is written — €3.25, not €3.250.
    await waitFor(() => expect(screen.getAllByText("€3.25").length).toBeGreaterThan(0));
  });

  it("rounds spend to milli, rather than showing the ledger's full precision", async () => {
    const tiny = report({
      rows: [{ ...report().rows[0]!, cost: "0.001497172000" }],
      totals: { ...report().totals, cost: "0.001497172000" },
    });
    vi.stubGlobal("fetch", respondWith(tiny));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getAllByText("€0.001").length).toBeGreaterThan(0));
    expect(screen.queryByText("€0.001497172")).not.toBeInTheDocument();
  });

  it("says 'less than' rather than telling someone they spent nothing", async () => {
    // Capping the decimals must not reintroduce the misleading zero that full
    // precision existed to avoid.
    const sliver = report({
      rows: [{ ...report().rows[0]!, cost: "0.000000400000" }],
      totals: { ...report().totals, cost: "0.000000400000" },
    });
    vi.stubGlobal("fetch", respondWith(sliver));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getAllByText("< €0.001").length).toBeGreaterThan(0));
  });

  it("names the period and its timezone", async () => {
    vi.stubGlobal("fetch", respondWith(report()));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getByText(/Spend · 2026-08/)).toBeInTheDocument());
    expect(screen.getByText(/Europe\/Rome/)).toBeInTheDocument();
  });

  it("renders the API's disclosures verbatim", async () => {
    // Wording a caveat twice — once in the API, once here — is how the two end
    // up disagreeing about what the number means.
    const note = "3 of 12 requests have estimated token counts: the provider did not report usage.";
    vi.stubGlobal("fetch", respondWith(report({ disclosures: [note] })));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getByText(note)).toBeInTheDocument());
  });

  it("asks for the current month by default", async () => {
    const fetchMock = respondWith(report());
    vi.stubGlobal("fetch", fetchMock);
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("period=2026-08"))).toBe(true);
  });

  it("says so plainly when there is no usage", async () => {
    const empty = report({
      rows: [],
      totals: { ...report().totals, requests: 0, total_tokens: 0, cost: "0" },
    });
    vi.stubGlobal("fetch", respondWith(empty));
    renderScreen(<Overview me={ME} />);

    await waitFor(() => expect(screen.getAllByText("€0.00").length).toBeGreaterThan(0));
  });

  describe("API keys", () => {
    const setup = () => userEvent.setup({ delay: null, advanceTimers: vi.advanceTimersByTime });

    async function openMintDialog(user: ReturnType<typeof userEvent.setup>) {
      await waitFor(() => expect(screen.getByText("API keys")).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: "New key" }));
      return within(await screen.findByRole("dialog"));
    }

    it("mints a key and shows the secret", async () => {
      const user = setup();
      const calls: Calls = { minted: [], revoked: [] };
      vi.stubGlobal("fetch", keyRoutes([], calls));
      renderScreen(<Overview me={ME} />);

      const dialog = await openMintDialog(user);
      await user.type(dialog.getByLabelText("Name"), "notebook");
      await user.click(dialog.getByRole("button", { name: "Create" }));

      await waitFor(() =>
        expect(screen.getByText("sk-live-THE-ONLY-COPY")).toBeInTheDocument(),
      );
      expect(calls.minted).toEqual([
        { name: "notebook", billing_group_id: null, expires_in_days: null },
      ]);
    });

    it("says the secret will not be shown again, and does not show it again", async () => {
      // The whole promise of the screen. The gateway keeps a hash, so a secret
      // that reappears anywhere would mean the console had kept a copy of the
      // one thing it must not keep.
      const user = setup();
      vi.stubGlobal("fetch", keyRoutes([]));
      renderScreen(<Overview me={ME} />);

      const dialog = await openMintDialog(user);
      await user.click(dialog.getByRole("button", { name: "Create" }));
      await waitFor(() =>
        expect(screen.getByText("sk-live-THE-ONLY-COPY")).toBeInTheDocument(),
      );
      expect(screen.getByText(/cannot be shown again/i)).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Done" }));
      const reopened = await openMintDialog(user);

      expect(screen.queryByText("sk-live-THE-ONLY-COPY")).not.toBeInTheDocument();
      // Back to the form, not a blank panel where the secret used to be.
      expect(reopened.getByRole("button", { name: "Create" })).toBeInTheDocument();
    });

    it("sends the chosen billing group and expiry", async () => {
      const user = setup();
      const calls: Calls = { minted: [], revoked: [] };
      vi.stubGlobal("fetch", keyRoutes([], calls));
      renderScreen(<Overview me={ME} />);

      const dialog = await openMintDialog(user);
      await user.selectOptions(dialog.getByLabelText("Billing group"), "g1");
      await user.selectOptions(dialog.getByLabelText("Expires"), "90");
      await user.click(dialog.getByRole("button", { name: "Create" }));

      await waitFor(() => expect(calls.minted.length).toBe(1));
      expect(calls.minted[0]).toEqual({
        name: "",
        billing_group_id: "g1",
        expires_in_days: 90,
      });
    });

    it("warns before the click when no group can be resolved", async () => {
      // The API refuses this with a good sentence, but a key that cannot be
      // billed is worth catching before it is asked for.
      const user = setup();
      vi.stubGlobal("fetch", keyRoutes([]));
      renderScreen(<Overview me={{ ...ME, default_billing_group: null }} />);

      // Scoped: the Groups stat tile says "No default billing group" too, and
      // the point here is that the *dialog* says it, where it can be acted on.
      const dialog = await openMintDialog(user);
      expect(dialog.getByText(/no default billing group/i)).toBeInTheDocument();
    });

    it("shows the API's refusal rather than a generic one", async () => {
      const user = setup();
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          if (String(input).includes("/reports/usage")) return jsonResponse(report());
          if (String(input).includes("/api/me/redaction")) return jsonResponse(redaction());
          if ((init?.method ?? "GET") === "POST") {
            return new Response(
              JSON.stringify({ error: { message: "Set a default billing group first." } }),
              { status: 400, headers: { "content-type": "application/json" } },
            );
          }
          return jsonResponse({ items: [], total: 0, limit: 200, offset: 0 });
        }),
      );
      renderScreen(<Overview me={ME} />);

      const dialog = await openMintDialog(user);
      await user.click(dialog.getByRole("button", { name: "Create" }));

      await waitFor(() =>
        expect(screen.getByText("Set a default billing group first.")).toBeInTheDocument(),
      );
    });

    it("asks before revoking, and says the bill is unaffected", async () => {
      const user = setup();
      const calls: Calls = { minted: [], revoked: [] };
      vi.stubGlobal("fetch", keyRoutes([apiKey()], calls));
      renderScreen(<Overview me={ME} />);

      await waitFor(() => expect(screen.getByText("laptop")).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: "Revoke" }));

      const dialog = within(await screen.findByRole("dialog"));
      expect(screen.getByText(/usage history is unaffected/i)).toBeInTheDocument();
      // Nothing sent on opening the confirmation.
      expect(calls.revoked).toEqual([]);

      await user.click(dialog.getByRole("button", { name: "Revoke key" }));
      await waitFor(() => expect(calls.revoked).toEqual(["k1"]));
    });

    it("offers nothing to revoke on a key that is already revoked", async () => {
      vi.stubGlobal("fetch", keyRoutes([apiKey({ revoked_at: "2026-08-10T10:00:00Z" })]));
      renderScreen(<Overview me={ME} />);

      await waitFor(() => expect(screen.getByText("Revoked")).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
    });
  });

  it("shows the error the API gave rather than a generic one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { message: "'nope' is not a period." } }), {
          status: 400,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    renderScreen(<Overview me={ME} />);

    // Twice, now that the redaction card reads from the API too: every card
    // that asked reports what it was told rather than one of them speaking for
    // the page.
    await waitFor(() =>
      expect(screen.getAllByText("'nope' is not a period.").length).toBeGreaterThan(0),
    );
  });

  describe("your own redaction", () => {
    it("shows what applies, and what everyone is held to", async () => {
      vi.stubGlobal("fetch", respondWith(report()));
      renderScreen(<Overview me={ME} />);

      await waitFor(() =>
        expect(screen.getByText("Applies now")).toBeInTheDocument(),
      );
      // Both documents, because they answer different questions: what happens
      // to my prompts, and what I am not allowed to go below.
      expect(
        screen.getAllByText("Anonymise, restore in the answer by default · 1 type · 1 allowed")
          .length,
      ).toBe(2);
      expect(
        screen.getByText("None. Your administrators' settings apply."),
      ).toBeInTheDocument();
    });

    it("lets a person protect more than the administrators require", async () => {
      const puts: unknown[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes("/api/me/redaction")) {
            if (init?.method === "PUT") {
              puts.push(JSON.parse(String(init.body)));
              return jsonResponse(redaction({ policy: redaction().baseline, rule_id: "r1" }));
            }
            return jsonResponse(redaction());
          }
          if (url.includes("/reports/usage")) return jsonResponse(report());
          return jsonResponse([]);
        }),
      );
      const user = userEvent.setup({ delay: null });
      renderScreen(<Overview me={ME} />);

      await user.selectOptions(await screen.findByLabelText(/PERSON/), "block");
      await user.type(screen.getByLabelText("Reason"), "clinical notes");
      await user.click(screen.getByRole("button", { name: "Save redaction" }));

      await waitFor(() => expect(puts).toHaveLength(1));
      const body = puts[0] as { policy: RedactionPolicy; reason: string };
      expect(body.policy.entities.PERSON!.mode).toBe("block");
      // Seeded from the baseline, not from an empty document: omitting a type
      // the administrators named is itself a weakening, and is refused.
      expect(body.policy.default_mode).toBe("anonymise_restore");
      expect(body.reason).toBe("clinical notes");
    });

    it("does not offer a mode weaker than the administrators set", async () => {
      // Disabled rather than absent: the floor is somebody else's decision, and
      // a select that silently omits three of five choices reads as a bug.
      vi.stubGlobal("fetch", respondWith(report()));
      renderScreen(<Overview me={ME} />);

      const modes = await screen.findByLabelText(/PERSON/);
      expect(within(modes).getByRole("option", { name: "Not redacted" })).toBeDisabled();
      expect(within(modes).getByRole("option", { name: "Redact" })).toBeEnabled();
    });

    it("shows the API's refusal, which names what weakened", async () => {
      // The comparison is against what the administrators set, which only the
      // API can see. Re-deriving "weaker" here would give two answers.
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes("/api/me/redaction")) {
            if (init?.method === "PUT") {
              return new Response(
                JSON.stringify({
                  error: {
                    message:
                      "This policy protects less than the one your administrators set, so it was not saved: PERSON. Your own policy may only tighten.",
                    code: "redaction_policy_weakens",
                  },
                }),
                { status: 400, headers: { "content-type": "application/json" } },
              );
            }
            return jsonResponse(redaction());
          }
          if (url.includes("/reports/usage")) return jsonResponse(report());
          return jsonResponse([]);
        }),
      );
      const user = userEvent.setup({ delay: null });
      renderScreen(<Overview me={ME} />);

      await waitFor(() => expect(screen.getByLabelText(/PERSON/)).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: "Save redaction" }));

      await waitFor(() =>
        expect(screen.getByText(/may only tighten/)).toBeInTheDocument(),
      );
      expect(screen.getByText("Your policy was not saved")).toBeInTheDocument();
    });

    it("offers no allow-list, which is the one field that can only weaken", async () => {
      vi.stubGlobal("fetch", respondWith(report()));
      renderScreen(<Overview me={ME} />);

      await waitFor(() => expect(screen.getByLabelText(/PERSON/)).toBeInTheDocument());
      expect(screen.queryByLabelText("Allowlist")).not.toBeInTheDocument();
    });
  });
});
