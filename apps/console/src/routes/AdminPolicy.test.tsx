import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../test-helpers";
import { AdminPolicy } from "./AdminPolicy";

afterEach(() => vi.unstubAllGlobals());

describe("A redaction policy page", () => {
  it("cannot pick a detector profile yet, and says why", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [], total: 0, limit: 200, offset: 0 })),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/admin/privacy/policies/new"]}>
          <Routes>
            <Route path="/admin/privacy/policies/:policyId" element={<AdminPolicy />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const picker = await screen.findByLabelText("Detector profile");
    expect(picker).toBeDisabled();
    expect(picker).toHaveAttribute("title", "Detector profiles on policies take effect in a later release.");
  });
});
