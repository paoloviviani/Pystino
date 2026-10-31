import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import stylesheet from "../index.css?raw";
import type { UsageReport, UsageReportRow } from "../lib/types";
import { jsonResponse } from "../test-helpers";
import { UsagePlots } from "./UsagePlots";
import type { PlotSplit } from "./UsagePlots";

function row(day: string, split: string, cost: string): UsageReportRow {
  return {
    key: day,
    label: day,
    split_key: split,
    split_label: split,
    requests: 2,
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 50,
    images: 0,
    searches: 0,
    own_searches: 0,
    unpriced_own_searches: 0,
    audio_seconds: "0",
    cost,
    native_cost: null,
    native_currency: null,
    estimated_requests: 0,
    unavailable_requests: 0,
    estimated_disconnected_requests: 0,
    via_alias_requests: 0,
  };
}

function report(rows: UsageReportRow[]): UsageReport {
  return {
    period: {
      kind: "month",
      label: "2026-03",
      start: "2026-02-28T23:00:00Z",
      end: "2026-03-31T22:00:00Z",
      timezone: "Europe/Rome",
    },
    group_by: "day",
    split_by: "provider",
    currency: "EUR",
    rows,
    totals: row("", "", "0"),
    disclosures: [],
    reconciliation: [],
  };
}

function Harness({ splits, seen }: { splits: PlotSplit[]; seen: string[] }) {
  return (
    <UsagePlots
      splits={splits}
      useChartReport={(split) =>
        // eslint-disable-next-line react-hooks/rules-of-hooks -- the prop is a hook
        useQuery({
          queryKey: ["plot", split],
          queryFn: async () => {
            seen.push(split);
            return (await fetch(`/api?split_by=${split}`)).json() as Promise<UsageReport>;
          },
        })
      }
    />
  );
}

function renderPlots(body: UsageReport, splits: PlotSplit[] = ["group", "user", "model", "provider"]) {
  const seen: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(body)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <Harness splits={splits} seen={seen} />
    </QueryClientProvider>,
  );
  return { ...utils, seen };
}

afterEach(() => vi.unstubAllGlobals());

describe("UsagePlots", () => {
  it("draws a named chart with a legend entry per series", async () => {
    renderPlots(report([row("2026-03-02", "fake", "3"), row("2026-03-02", "other-cloud", "1")]));
    const chart = await screen.findByRole("img");
    expect(chart).toHaveAccessibleName(/Spend per day, by group, 2026-03/);
    const legend = screen.getByRole("list", { name: /legend/i });
    expect(legend).toHaveTextContent("fake");
    expect(legend).toHaveTextContent("other-cloud");
  });

  it("offers exactly the breakdowns it was given, and refetches on change", async () => {
    const user = userEvent.setup({ delay: null });
    const { seen } = renderPlots(report([row("2026-03-02", "fake", "3")]), [
      "model",
      "provider",
      "group",
    ]);
    const select = await screen.findByLabelText("Chart breakdown");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.value);
    expect(options).toEqual(["model", "provider", "group"]);
    await user.selectOptions(select, "provider");
    await waitFor(() => expect(seen).toContain("provider"));
  });

  it("switches metric", async () => {
    const user = userEvent.setup({ delay: null });
    renderPlots(report([row("2026-03-02", "fake", "3")]));
    await user.selectOptions(await screen.findByLabelText("Show"), "tokens");
    expect(await screen.findByRole("img")).toHaveAccessibleName(/Tokens per day/);
  });

  it("writes chart amounts to the cent", async () => {
    const user = userEvent.setup({ delay: null });
    renderPlots(report([row("2026-03-02", "fake", "56.936")]));
    await user.click(await screen.findByRole("button", { name: "Show as a table" }));
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("€56.94");
    expect(table).not.toHaveTextContent("€56.936");
  });

  it("has a table with the plotted figures", async () => {
    const user = userEvent.setup({ delay: null });
    renderPlots(report([row("2026-03-02", "fake", "3")]));
    await user.click(await screen.findByRole("button", { name: "Show as a table" }));
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("2026-03-02");
    expect(table).toHaveTextContent("€3");
    expect(screen.getByRole("button", { name: "Hide the table" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("says plainly when there is nothing to chart", async () => {
    renderPlots(report([]));
    expect(await screen.findByText("No usage to chart")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("folds series beyond six into Other", async () => {
    const rows = Array.from({ length: 9 }, (_, i) => row("2026-03-02", `p${i}`, String(20 - i)));
    renderPlots(report(rows));
    const legend = await screen.findByRole("list", { name: /legend/i });
    expect(legend.querySelectorAll("li")).toHaveLength(7);
    expect(legend).toHaveTextContent("Other");
  });

  it("colours through theme tokens, so dark mode re-themes it", async () => {
    // Literal colours in the SVG would ignore the `.dark` class. The palette is
    // `--plot-N`, and the stylesheet redefines every one of them under `.dark`.
    const { container } = renderPlots(report([row("2026-03-02", "fake", "3")]));
    await screen.findByRole("img");
    const fills = Array.from(container.querySelectorAll("svg rect")).map((r) =>
      r.getAttribute("fill"),
    );
    expect(fills.length).toBeGreaterThan(0);
    expect(fills.every((fill) => /^var\(--plot-/.test(fill ?? ""))).toBe(true);

    const dark = stylesheet.slice(stylesheet.indexOf(".dark {"));
    for (const name of ["1", "2", "3", "4", "5", "6", "other"]) {
      expect(dark).toContain(`--plot-${name}:`);
    }
  });
});
