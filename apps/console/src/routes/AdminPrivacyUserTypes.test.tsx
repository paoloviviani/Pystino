import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdminPrivacyDetectors } from "./AdminPrivacyDetectors";

/** "What users may add": a multi-select of what the detectors support, not a free-text list. */

const GROUPS = [
  { detector: "patterns", types: ["EMAIL_ADDRESS", "IBAN_CODE"] },
  { detector: "spacy", types: ["LOCATION", "PERSON"] },
  { detector: "gliner", types: ["MEDICATION", "PERSON"] },
];

interface Put {
  body: { user_entity_types: string[] | null };
}

function stub(opts: {
  stored?: string[] | null;
  groups?: typeof GROUPS | null;
  putResponse?: () => Response;
}) {
  const puts: Put[] = [];
  const json = (body: unknown, code = 200) =>
    new Response(JSON.stringify(body), { status: code, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/privacy/settings")) {
        puts.push({ body: JSON.parse(String(init.body)) });
        return opts.putResponse ? opts.putResponse() : json({ user_entity_types: [] });
      }
      if (url.includes("/privacy/settings")) {
        return json({ user_entity_types: opts.stored === undefined ? [] : opts.stored, unknown_entity_types: [] });
      }
      if (url.includes("/privacy/options")) {
        return json({ supported_groups: opts.groups === undefined ? GROUPS : opts.groups });
      }
      if (url.includes("/privacy/engines")) {
        return json({ reachable: true, detail: "ok", pattern_entities: [], engines: [], patterns_enabled: true, patterns_used_by: [] });
      }
      return json({});
    }),
  );
  return puts;
}

function renderUi() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPrivacyDetectors />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("What users may add", () => {
  it("renders the server's types, by detector, as checkboxes (no free-text list)", async () => {
    stub({});
    renderUi();
    const patterns = await screen.findByTestId("types-patterns");
    expect(within(patterns).getByRole("checkbox", { name: /IBAN_CODE/ })).toBeInTheDocument();
    expect(within(screen.getByTestId("types-gliner")).getByRole("checkbox", { name: /MEDICATION/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Entity types users may name")).toBeNull();
  });

  it("filters by search", async () => {
    stub({});
    renderUi();
    await screen.findByTestId("types-patterns");
    await userEvent.type(screen.getByLabelText("Search entity types"), "medic");
    expect(screen.queryByTestId("types-patterns")).toBeNull();
    expect(screen.getByTestId("types-gliner")).toBeInTheDocument();
  });

  it("saves the ticked types, and none ticked saves an empty list", async () => {
    const puts = stub({ stored: ["PERSON"] });
    renderUi();
    await screen.findByTestId("chosen-PERSON");
    const patterns = await screen.findByTestId("types-patterns");
    await userEvent.click(within(patterns).getByRole("checkbox", { name: /IBAN_CODE/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toEqual({ user_entity_types: ["PERSON", "IBAN_CODE"] });
    await userEvent.click(screen.getByRole("button", { name: "Remove PERSON" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    // Nothing ticked under "only" is none, never "any".
    expect(puts[1]!.body).toEqual({ user_entity_types: [] });
  });

  it("shows 'any' for a stored null and offers no list", async () => {
    stub({ stored: null });
    renderUi();
    expect(await screen.findByRole("radio", { name: "Any type the detectors offer" })).toBeChecked();
    expect(screen.queryByTestId("types-patterns")).toBeNull();
  });

  it("switching to any from a non-empty list asks first, then saves null", async () => {
    const puts = stub({ stored: ["PERSON"] });
    renderUi();
    await screen.findByTestId("chosen-PERSON");
    await userEvent.click(screen.getByRole("radio", { name: "Any type the detectors offer" }));
    expect(screen.getByRole("radio", { name: "Only the types I pick" })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Keep the list" }));
    expect(screen.queryByText(/Switch to any type/)).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: "Any type the detectors offer" }));
    await userEvent.click(screen.getByRole("button", { name: "Allow any type" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toEqual({ user_entity_types: null });
  });

  it("removing the last chip and saving does not mean any", async () => {
    const puts = stub({ stored: ["IBAN_CODEE"] });
    renderUi();
    await userEvent.click(await screen.findByRole("button", { name: "Remove IBAN_CODEE" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toEqual({ user_entity_types: [] });
  });

  it("shows a stored type no detector supports as unknown, and removing it leaves the rest", async () => {
    const puts = stub({ stored: ["PERSON", "IBAN_CODEE"] });
    renderUi();
    const chip = await screen.findByTestId("chosen-IBAN_CODEE");
    expect(within(chip).getByText(/not offered by any detector/)).toBeInTheDocument();
    expect(within(screen.getByTestId("chosen-PERSON")).queryByText(/not offered/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Remove IBAN_CODEE" }));
    expect(screen.queryByTestId("chosen-IBAN_CODEE")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toEqual({ user_entity_types: ["PERSON"] });
  });

  it("shows the server's refusal", async () => {
    stub({
      stored: ["PERSON"],
      putResponse: () =>
        new Response(JSON.stringify({ error: { message: "No detector supports these entity types: X." } }), {
          status: 422,
          headers: { "content-type": "application/json" },
        }),
    });
    renderUi();
    await screen.findByTestId("chosen-PERSON");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/No detector supports these entity types/)).toBeInTheDocument();
  });

  it("with the service silent lists nothing to pick, says so, and marks nothing unknown", async () => {
    stub({ stored: ["PERSON"], groups: null });
    renderUi();
    expect(await screen.findByText(/cannot be listed or checked/)).toBeInTheDocument();
    expect(screen.getByTestId("chosen-PERSON")).toBeInTheDocument();
    expect(screen.queryByText(/not offered by any detector/)).toBeNull();
  });
});
