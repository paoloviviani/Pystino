import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

function Boom(): never {
  throw new Error("status is only supported for assistant messages");
}

describe("ErrorBoundary", () => {
  it("shows the real message rather than a blank page", () => {
    // React logs the caught error; silenced so the test output stays readable.
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // The exact message, because a generic apology would make the next bug
    // report as unactionable as the one that prompted this component.
    expect(
      screen.getByText("status is only supported for assistant messages"),
    ).toBeInTheDocument();
  });

  it("renders children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>fine</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("fine")).toBeInTheDocument();
  });
});
