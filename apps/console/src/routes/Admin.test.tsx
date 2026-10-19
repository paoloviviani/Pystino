import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { Admin } from "./Admin";

/**
 * The administration landing page.
 *
 * The order of the cards is the only thing asserted, and it is worth asserting
 * because it is the kind of thing that drifts: the list is edited whenever a
 * screen is added, and the header's nav — which has to agree with it — lives in
 * another file.
 */
describe("Admin", () => {
  it("puts Settings last, as the header's nav does", () => {
    render(
      <MemoryRouter>
        <Admin />
      </MemoryRouter>,
    );

    const titles = screen.getAllByRole("link").map((link) => link.textContent ?? "");
    expect(titles.at(-1)).toContain("Settings");
    // Usage leads: the deployment's spend is what an administrator opens this
    // section to look at.
    expect(titles.at(0)).toContain("Usage");
  });

  it("carries Routing beside Models, at /admin/routing as the header's nav does", () => {
    render(
      <MemoryRouter>
        <Admin />
      </MemoryRouter>,
    );

    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    // Routing shipped after this page existed (ADR 0101), and a section the
    // landing page omits is one administrators do not learn exists.
    const routing = hrefs.indexOf("/admin/routing");
    expect(routing).toBeGreaterThan(-1);
    // Beside Models, not under it — the same adjacency the header's nav holds,
    // because an alias is a name for a model (components/Shell.tsx).
    expect(hrefs.indexOf("/admin/models")).toBeLessThan(routing);
    expect(hrefs.indexOf("/admin/users")).toBeGreaterThan(routing);
  });
});
