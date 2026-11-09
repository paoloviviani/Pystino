import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Tip } from "./Tip";

describe("Tip", () => {
  it("opens on a click and closes on the next", async () => {
    render(<Tip text="Why this exists" />);
    const button = screen.getByRole("button", { name: "Why this exists" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(button);
    expect(await screen.findByText("Why this exists", { selector: "div" })).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(button);
    await Promise.resolve();
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes on Escape", async () => {
    render(<Tip text="Escape me" />);
    const button = screen.getByRole("button", { name: "Escape me" });
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(document.activeElement ?? button, { key: "Escape" });
    await Promise.resolve();
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });
});
