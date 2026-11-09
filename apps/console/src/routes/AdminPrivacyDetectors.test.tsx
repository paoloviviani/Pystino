import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PrivacyMasterSwitch } from "../components/PrivacyMasterSwitch";
import { PrivacyHeader } from "../components/PrivacyHeader";
import type { PrivacyEngine, PrivacyEngines } from "../lib/types";
import { AdminPrivacyDetectors } from "./AdminPrivacyDetectors";

/** The Detectors tab, the master switch and the off banner, against capability fixtures. */

const engine = (name: "spacy" | "gliner", over: Partial<PrivacyEngine> = {}): PrivacyEngine => ({
  name,
  available: true,
  default: name === "spacy",
  enabled: true,
  used_by: [],
  entities: name === "spacy" ? ["PERSON", "LOCATION"] : ["PERSON", "MEDICATION"],
  custom_labels: name === "gliner",
  labels: [],
  ms_per_token: name === "spacy" ? 0.3 : 14.4,
  languages: ["en", "it"],
  ...over,
});

const base = {
  reachable: true,
  detail: "ok",
  patterns_enabled: true,
  patterns_used_by: [] as string[],
  pattern_entities: ["EMAIL_ADDRESS", "IBAN_CODE", "URL"],
};

const PATTERN_ONLY: PrivacyEngines = {
  ...base,
  engines: [engine("spacy", { available: false, entities: [] }), engine("gliner", { available: false, entities: [] })],
};
const SPACY: PrivacyEngines = {
  ...base,
  engines: [engine("spacy"), engine("gliner", { available: false, entities: [] })],
};
const BOTH: PrivacyEngines = { ...base, engines: [engine("spacy"), engine("gliner")] };

function status(over: Record<string, unknown> = {}) {
  return {
    engine: "http",
    enabled: true,
    endpoint: "http://redaction:8080",
    installed_engines: ["noop", "http"],
    engines: [
      { name: "http", label: "Presidio", description: "", needs_endpoint: true, redacts: true, is_active: true, blocked_reason: null },
      { name: "noop", label: "Off", description: "", needs_endpoint: false, redacts: false, is_active: false, blocked_reason: null },
    ],
    source: "environment",
    configured: null,
    presidio_pattern_matching: null,
    presidio_ner: null,
    patterns_enabled: true,
    spacy_enabled: true,
    gliner_enabled: true,
    propagation_seconds: 10,
    warnings: [],
    fail_open: false,
    restore_in_response: true,
    restore_in_tool_arguments: true,
    language: "en",
    score_threshold: 0.5,
    entity_types: null,
    policy: { default_mode: "off", engine: null, formats: {}, entities: {}, custom_entities: [], patterns: [], keywords: [], allow_list: [] },
    policy_source: "environment",
    timeout_seconds: 5,
    cache_size: 10,
    placeholder_key_set: true,
    service: null,
    activity: { window_seconds: 86400, requests: 0, requests_redacted: 0, entities_redacted: 0, engines: [] },
    ...over,
  };
}

interface Put {
  url: string;
  body: Record<string, unknown>;
}

function stub(opts: { status?: unknown; engines?: PrivacyEngines; putResponse?: () => Response }) {
  const puts: Put[] = [];
  const json = (body: unknown, code = 200) =>
    new Response(JSON.stringify(body), { status: code, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PUT" && url.includes("/redaction/engine")) {
        puts.push({ url, body: JSON.parse(String(init.body)) });
        return opts.putResponse ? opts.putResponse() : json(opts.status ?? status());
      }
      if (url.includes("/privacy/engines")) return json(opts.engines ?? BOTH);
      if (url.includes("/privacy/settings")) return json({ user_entity_types: null });
      if (url.includes("/admin/redaction")) return json(opts.status ?? status());
      return json({});
    }),
  );
  return puts;
}

function renderUi(element: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Detectors: the cards", () => {
  it("on a pattern-only service shows the patterns, and says GLiNER and spaCy are not installed", async () => {
    stub({ engines: PATTERN_ONLY });
    renderUi(<AdminPrivacyDetectors />);
    const patterns = await screen.findByTestId("detector-patterns");
    expect(within(patterns).getByText(/3 formats/)).toBeInTheDocument();
    for (const which of ["spacy", "gliner"]) {
      const card = screen.getByTestId(`detector-${which}`);
      expect(within(card).getByText("Not installed")).toBeInTheDocument();
      expect(within(card).getByRole("link", { name: "Read how" })).toBeInTheDocument();
      expect(within(card).getByRole("switch")).toBeDisabled();
    }
    expect(screen.getByText(/REDACTION_NLP_ENGINE=spacy/)).toBeInTheDocument();
    expect(screen.getByText(/GLINER_MODEL/)).toBeInTheDocument();
  });

  it("with spaCy shows its models, speed and entity types, and GLiNER not installed", async () => {
    stub({ engines: SPACY });
    renderUi(<AdminPrivacyDetectors />);
    const spacy = await screen.findByTestId("detector-spacy");
    expect(within(spacy).getByText("0.3 ms per token")).toBeInTheDocument();
    expect(within(spacy).getByText(/Languages: en, it/)).toBeInTheDocument();
    expect(within(spacy).getByText(/Entity types:.*Person/)).toBeInTheDocument();
    expect(within(spacy).queryByText("Custom types allowed")).toBeNull();
    expect(within(screen.getByTestId("detector-gliner")).getByText("Not installed")).toBeInTheDocument();
  });

  it("with GLiNER shows that custom types are allowed and who uses each detector", async () => {
    const engines: PrivacyEngines = {
      ...BOTH,
      patterns_used_by: ["Default"],
      engines: [engine("spacy"), engine("gliner", { used_by: ["Health", "Names"] })],
    };
    stub({ engines });
    renderUi(<AdminPrivacyDetectors />);
    const gliner = await screen.findByTestId("detector-gliner");
    expect(within(gliner).getByText("Custom types allowed")).toBeInTheDocument();
    expect(within(gliner).getByText("14.4 ms per token")).toBeInTheDocument();
    expect(within(gliner).getByText(/used by 2 policies: Health, Names/)).toBeInTheDocument();
    expect(within(screen.getByTestId("detector-patterns")).getByText(/used by 1 policies/)).toBeInTheDocument();
    expect(within(screen.getByTestId("detector-spacy")).getByText("not used by any policy")).toBeInTheDocument();
  });

  it("switches one detector and keeps the others as they are", async () => {
    const user = userEvent.setup({ delay: null });
    // The previous change had its own reason, which must not be copied onto this one.
    const puts = stub({
      engines: BOTH,
      status: status({ configured: { engine: "http", reason: "an older reason", changed_at: "", changed_by: null, presidio_ner: null, presidio_pattern_matching: null, spacy_enabled: null, gliner_enabled: null } }),
    });
    renderUi(<AdminPrivacyDetectors />);
    const card = await screen.findByTestId("detector-gliner");
    await user.click(within(card).getByRole("switch"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toMatchObject({
      engine: "http",
      presidio_pattern_matching: true,
      spacy_enabled: true,
      gliner_enabled: false,
      reason: "Switched GLiNER Off from the console",
    });
  });

  it("shows the refusal, naming the policies, when a detector is in use", async () => {
    const user = userEvent.setup({ delay: null });
    stub({
      engines: BOTH,
      putResponse: () =>
        new Response(
          JSON.stringify({
            error: {
              message: "spaCy cannot be switched off: used by Names, Health. Deactivate those policies first.",
            },
          }),
          { status: 409, headers: { "content-type": "application/json" } },
        ),
    });
    renderUi(<AdminPrivacyDetectors />);
    const card = await screen.findByTestId("detector-spacy");
    await user.click(within(card).getByRole("switch"));
    expect(await screen.findByText(/used by Names, Health/)).toBeInTheDocument();
    // The switch did not move: the status is still the server's.
    expect(within(card).getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("keeps the setting for what users may add, and the technical section collapsed", async () => {
    stub({});
    renderUi(<AdminPrivacyDetectors />);
    expect(await screen.findByLabelText("Entity types users may name")).toBeInTheDocument();
    const details = (await screen.findByText("Engine activity and configuration")).closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
  });
});

describe("the master switch and the off banner", () => {
  it("turns privacy off only after an inline confirmation, carrying the detector switches", async () => {
    const user = userEvent.setup({ delay: null });
    const puts = stub({ status: status({ spacy_enabled: false }) });
    renderUi(<PrivacyMasterSwitch />);
    const toggle = await screen.findByRole("switch", { name: "Privacy" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    // Nothing has been sent: the confirmation comes first.
    expect(puts).toHaveLength(0);
    expect(screen.getByText("Turn privacy off?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(puts).toHaveLength(0);
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Turn privacy off" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toMatchObject({
      engine: "noop",
      spacy_enabled: false,
      gliner_enabled: true,
      reason: "Turned privacy off from the console",
    });
  });

  it("turns privacy on without asking, restoring the engine that was running", async () => {
    const user = userEvent.setup({ delay: null });
    const base = status({ enabled: false, engine: "noop", restore_engine: "regex" });
    const puts = stub({
      status: {
        ...base,
        engines: [
          ...(base.engines as object[]),
          { name: "regex", label: "Regex", description: "", needs_endpoint: false, redacts: true, is_active: false, blocked_reason: null },
        ],
      },
    });
    renderUi(<PrivacyMasterSwitch />);
    const toggle = await screen.findByRole("switch", { name: "Privacy" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await user.click(toggle);
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.body).toMatchObject({
      engine: "regex",
      reason: "Turned privacy on from the console",
    });
  });

  it("shows a warning banner across the Privacy area while it is off, and not while it is on", async () => {
    stub({ status: status({ enabled: false, engine: "noop" }) });
    const off = renderUi(<PrivacyHeader tab="policies" />);
    expect(await screen.findByText("Privacy is off")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Turn it on" })).toHaveAttribute("href", "/admin/privacy");
    off.unmount();
    stub({});
    renderUi(<PrivacyHeader tab="policies" />);
    await screen.findByRole("link", { name: "Detectors" });
    expect(screen.queryByText("Privacy is off")).toBeNull();
  });
});
