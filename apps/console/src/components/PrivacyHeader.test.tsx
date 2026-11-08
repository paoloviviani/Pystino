import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { PrivacyHeader } from "./PrivacyHeader";

function renderHeader(tab: "rules" | "hidden" | "detectors" | "test") {
  render(
    <MemoryRouter>
      <PrivacyHeader tab={tab} />
    </MemoryRouter>,
  );
}

describe("PrivacyHeader", () => {
  it("has four tabs, each a link, with the current one marked", () => {
    renderHeader("hidden");
    const tabs = within(screen.getByRole("navigation", { name: "Privacy sections" }));
    expect(tabs.getAllByRole("link").map((l) => [l.textContent, l.getAttribute("href")])).toEqual([
      ["Rules", "/admin/privacy"],
      ["What gets hidden", "/admin/privacy/hidden"],
      ["Detectors", "/admin/privacy/detectors"],
      ["Test", "/admin/privacy/test"],
    ]);
    expect(tabs.getByRole("link", { name: "What gets hidden" })).toHaveAttribute("aria-current", "page");
    expect(tabs.getByRole("link", { name: "Rules" })).not.toHaveAttribute("aria-current");
  });

  it("states the order and how each step combines its rules", () => {
    renderHeader("rules");
    const steps = within(screen.getByRole("navigation", { name: "The four steps of the privacy pipeline" }));
    expect(steps.getByText(/first match wins/)).toBeInTheDocument();
    expect(steps.getByText(/every matching rule applies, strictest wins/)).toBeInTheDocument();
    expect(steps.getByRole("link", { name: /^1 Route/ })).toHaveAttribute("href", "/admin/privacy");
    expect(steps.getByRole("link", { name: /^2 Detect/ })).toHaveAttribute("href", "/admin/privacy/detectors");
    expect(steps.getByRole("link", { name: /^3 Hide/ })).toHaveAttribute("href", "/admin/privacy/hidden");
    expect(screen.getByText(/Routing never lowers a redaction mode/)).toBeInTheDocument();
  });
});
