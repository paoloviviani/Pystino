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
});
