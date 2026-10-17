import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MessagePart } from "../paraglide/runtime.js";
import { Message } from "./Message";

/**
 * A message with a link in it is one string, and the tag inside it is where a
 * translator puts it. This is what turns the compiled parts back into React —
 * so what has to hold is that the sentence comes out whole, that the tag
 * arrives at the component the screen named, and that an unexpected tag costs
 * the decoration rather than the words.
 *
 * The parts are written out by hand rather than taken from a compiled
 * message: what is under test is this component's reading of the part list,
 * and a fixture that cannot change when a screen is converted says more about
 * that than one that does. The routes that use it render real messages.
 */
const WITH_LINK: MessagePart[] = [
  { type: "text", value: "No such address in the console. " },
  { type: "markup-start", name: "link", options: {}, attributes: {} },
  { type: "text", value: "Back to your usage" },
  { type: "markup-end", name: "link", options: {}, attributes: {} },
  { type: "text", value: "." },
];

const message = (parts: MessagePart[]) => ({ parts: () => parts });

describe("Message", () => {
  it("renders the message's text and its tag, in the order the message says", () => {
    render(<Message message={message(WITH_LINK)} markup={{ link: (c) => <a href="/">{c}</a> }} />);
    expect(screen.getByRole("link", { name: "Back to your usage" }).getAttribute("href")).toBe("/");
    expect(document.body.textContent).toBe("No such address in the console. Back to your usage.");
  });

  it("keeps the words when a tag has no renderer, rather than dropping them", () => {
    render(<Message message={message(WITH_LINK)} markup={{}} />);
    expect(document.body.textContent).toBe("No such address in the console. Back to your usage.");
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("nests a tag inside a tag", () => {
    const nested: MessagePart[] = [
      { type: "text", value: "before " },
      { type: "markup-start", name: "outer", options: {}, attributes: {} },
      { type: "text", value: "inside " },
      { type: "markup-start", name: "inner", options: {}, attributes: {} },
      { type: "text", value: "deep" },
      { type: "markup-end", name: "inner", options: {}, attributes: {} },
      { type: "markup-end", name: "outer", options: {}, attributes: {} },
      { type: "text", value: " after" },
    ];
    render(
      <Message
        message={message(nested)}
        markup={{
          outer: (c) => <strong>{c}</strong>,
          inner: (c) => <em>{c}</em>,
        }}
      />,
    );
    expect(document.body.textContent).toBe("before inside deep after");
    expect(screen.getByText("deep").tagName).toBe("EM");
  });

  it("renders a tag that stands alone with no children of its own", () => {
    const standalone: MessagePart[] = [
      { type: "text", value: "Settings " },
      { type: "markup-standalone", name: "icon", options: {}, attributes: {} },
    ];
    render(
      <Message message={message(standalone)} markup={{ icon: () => <span aria-label="gear" /> }} />,
    );
    expect(screen.getByLabelText("gear")).toBeTruthy();
  });
});
