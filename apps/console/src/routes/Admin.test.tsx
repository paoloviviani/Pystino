import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { Admin } from "./Admin";

/**
 * The administration landing page.
 *
 * The page's whole shape is the grouping: five categories, each naming the
 * question its cards answer, in an order the sidebar repeats (both read
 * lib/adminNav.ts, so a drift between them is a code change away — but the
 * order itself is still worth pinning, because it is edited whenever a screen
 * is added).
 */
describe("Admin", () => {
  it("groups the sections under five headings, in the sidebar's order", () => {
    render(
      <MemoryRouter>
        <Admin />
      </MemoryRouter>,
    );

    // Each named section is one group; its heading is the h2 inside it. (The
    // cards' own titles are h2s too, which is why the groups are located as
    // regions rather than by heading level.)
    const headings = screen
      .getAllByRole("region")
      .map((region) => region.querySelector("h2")?.textContent ?? "");
    expect(headings).toEqual(["Usage & limits", "Serving", "Privacy", "People", "Deployment"]);
  });

  it("carries every screen exactly once, in the grouped order", () => {
    render(
      <MemoryRouter>
        <Admin />
      </MemoryRouter>,
    );

    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual([
      // Usage & limits: the deployment's spend is what an administrator opens
      // this page to look at, so it leads.
      "/admin/reports",
      "/admin/quotas",
      // Serving: where requests go and what they cost, web search with them
      // (a search backend is a provider).
      "/admin/providers",
      "/admin/models",
      "/admin/routing",
      "/admin/search",
      // Privacy.
      "/admin/redaction",
      // People.
      "/admin/users",
      "/admin/groups",
      // Deployment last: the wiring of the deployment, not its traffic — the
      // one position that would read as "start with this" if it led.
      "/admin/settings",
    ]);
  });
});
