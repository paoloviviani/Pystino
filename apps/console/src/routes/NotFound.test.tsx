import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { NotFound } from "./NotFound";

/**
 * What this screen must get right now that its two strings live in a
 * catalogue: the English is the English it was, byte for byte, and the link
 * is still a link the router owns.
 */
describe("NotFound", () => {
  it("shows the same sentence, with the link inside it", () => {
    render(
      <MemoryRouter>
        <NotFound />
      </MemoryRouter>,
    );
    expect(screen.getByText("No such page")).toBeTruthy();
    expect(document.body.textContent).toContain("No such address in the console. Back to your usage.");
    expect(screen.getByRole("link", { name: "Back to your usage" }).getAttribute("href")).toBe("/");
  });
});
