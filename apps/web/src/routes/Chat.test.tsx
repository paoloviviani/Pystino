/**
 * Rendering a conversation.
 *
 * This file exists because the first assistant-ui integration rendered, threw,
 * and left a blank page — the app "flashed and disappeared" on login. There was
 * no test that mounted the component at all, so nothing could have caught it.
 * A render test is the cheapest thing that would have.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Chat } from "./Chat";
import type { ConversationDetail, Message } from "../lib/api";

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    position: 0,
    role: "user",
    content: "hello",
    reasoning: null,
    status: "complete",
    model: null,
    request_id: null,
    error: null,
    usage: null,
    created_at: "2026-08-28T12:00:00Z",
    ...overrides,
  };
}

function conversation(messages: Message[]): ConversationDetail {
  return {
    id: "c1",
    title: "A conversation",
    model: "test-model",
    created_at: "2026-08-28T12:00:00Z",
    updated_at: "2026-08-28T12:00:00Z",
    messages,
  };
}

function serve(detail: ConversationDetail) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(detail), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

const MODELS = [{ id: "test-model", owned_by: "test" }];

describe("Chat", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders an empty conversation", async () => {
    serve(conversation([]));
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await screen.findByText("A conversation");
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
  });

  it("renders a transcript", async () => {
    serve(
      conversation([
        message({ id: "m1", role: "user", content: "what is two plus two" }),
        message({ id: "m2", position: 1, role: "assistant", content: "Four." }),
      ]),
    );
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await waitFor(() => expect(screen.getByText(/what is two plus two/)).toBeInTheDocument());
    expect(screen.getByText(/Four\./)).toBeInTheDocument();
  });

  it("collapses a finished message's reasoning, and opens on request", async () => {
    // Closed once it has stopped, which is assistant-ui's own behaviour and the
    // right one: thinking is interesting as it arrives and noise afterwards,
    // and it is usually longer than the answer it explains.
    const user = userEvent.setup({ delay: null });
    serve(
      conversation([
        message({
          id: "m2",
          role: "assistant",
          content: "Four.",
          reasoning: "two and two",
        }),
      ]),
    );
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Four\./)).toBeInTheDocument());

    // The disclosure is there and the thinking is not in the document yet.
    const trigger = screen.getByRole("button", { name: /Thought/ });
    expect(screen.queryByText(/two and two/)).not.toBeInTheDocument();

    await user.click(trigger);
    expect(screen.getByText(/two and two/)).toBeInTheDocument();
  });

  it("shows no disclosure for a message that did no thinking", async () => {
    serve(conversation([message({ id: "m2", role: "assistant", content: "Four." })]));
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Four\./)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Thought/ })).not.toBeInTheDocument();
  });

  it("does not remount the transcript while a turn streams", async () => {
    // The flicker bug, pinned by DOM node identity rather than by text.
    //
    // Defining the message components inline in the JSX made them a new
    // component *type* on every render, and Chat re-renders on every streamed
    // token — so React unmounted and remounted the whole transcript per delta.
    // The text was always correct; it simply disappeared and came back dozens
    // of times a second. Nothing that asserts on content can see that. A node
    // that survives an update can.
    serve(
      conversation([
        message({ id: "m1", role: "user", content: "question" }),
        message({ id: "m2", position: 1, role: "assistant", content: "first" }),
      ]),
    );
    const { rerender } = render(
      <Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />,
    );
    const before = await screen.findByText("question");

    // Any re-render of the parent is enough: if the component types are
    // unstable, this alone replaces the node.
    rerender(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    const after = screen.getByText("question");

    expect(after).toBe(before);
  });

  it("offers the models it was given", async () => {
    serve(conversation([]));
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Model")).toBeInTheDocument());
  });
});
