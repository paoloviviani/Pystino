import { DEFAULT_UI_MESSAGES, Dialog, Pagination, Spinner, Table, UiMessagesProvider } from "@llmp/ui";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setLocale } from "../paraglide/runtime.js";
import { uiMessages } from "./uiMessages";

afterEach(() => setLocale("en", { reload: false }));

function Everything() {
  return (
    <>
      <Pagination total={120} limit={50} offset={50} onOffsetChange={() => {}} />
      <Pagination total={0} limit={50} offset={50} onOffsetChange={() => {}} noun="users" />
      <Spinner />
      <Table columns={[]} rows={[]} rowKey={(_, i) => String(i)} />
    </>
  );
}

/** Apart from the rest: an open modal makes everything behind it inert. */
function Modal() {
  return (
    <Dialog open title="Title" onClose={() => {}}>
      body
    </Dialog>
  );
}

describe("shared components and the message catalogue", () => {
  it("speak English with no provider, as they did before", () => {
    render(<Everything />);
    expect(screen.getAllByRole("navigation", { name: "Pagination" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Next" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Previous" })).toHaveLength(2);
    expect(screen.getByText("of 120 rows")).toBeInTheDocument();
    expect(screen.getByText("No users")).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.getByText("Nothing to show.")).toBeInTheDocument();
    render(<Modal />);
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("English catalogue matches the package's own defaults", () => {
    const fromCatalogue = uiMessages();
    for (const key of Object.keys(DEFAULT_UI_MESSAGES) as (keyof typeof fromCatalogue)[]) {
      const a = fromCatalogue[key];
      const b = DEFAULT_UI_MESSAGES[key];
      if (typeof a === "string") expect(a).toBe(b);
    }
    expect(fromCatalogue.paginationNone("users")).toBe(DEFAULT_UI_MESSAGES.paginationNone("users"));
    expect(fromCatalogue.paginationOf("7", "users")).toBe(DEFAULT_UI_MESSAGES.paginationOf("7", "users"));
  });

  it("render the pseudo-locale, so a word left in the package would show unbracketed", () => {
    setLocale("en-XA", { reload: false });
    render(
      <UiMessagesProvider messages={uiMessages()}>
        <Everything />
      </UiMessagesProvider>,
    );
    const bracketed = (text: string) => expect(text).toMatch(/^\[.+\]$/);
    bracketed(uiMessages().paginationNext);
    // None of the English survives anywhere a person can read it.
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Previous" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing to show.")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: uiMessages().paginationNext })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: uiMessages().paginationPrevious })).toHaveLength(2);
    expect(screen.getAllByRole("navigation", { name: uiMessages().paginationLabel })).toHaveLength(2);
    expect(screen.getByText(`${uiMessages().loading}…`)).toBeInTheDocument();
    expect(screen.getByText(uiMessages().tableEmpty)).toBeInTheDocument();
    expect(screen.getByText(uiMessages().paginationNone("users"))).toBeInTheDocument();
    expect(screen.getByText(uiMessages().paginationOf("120", uiMessages().paginationRows))).toBeInTheDocument();
  });

  it("render the pseudo-locale in the dialog's close button", () => {
    setLocale("en-XA", { reload: false });
    render(
      <UiMessagesProvider messages={uiMessages()}>
        <Modal />
      </UiMessagesProvider>,
    );
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: uiMessages().close })).toBeInTheDocument();
  });

  it("let a prop beat the provider", () => {
    render(
      <UiMessagesProvider messages={uiMessages()}>
        <Spinner label="Signing you in" />
        <Table columns={[]} rows={[]} rowKey={(_, i) => String(i)} empty="No quota rules." />
      </UiMessagesProvider>,
    );
    expect(screen.getByText("Signing you in…")).toBeInTheDocument();
    expect(screen.getByText("No quota rules.")).toBeInTheDocument();
  });
});
