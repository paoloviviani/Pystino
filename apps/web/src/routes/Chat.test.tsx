/**
 * Rendering a conversation.
 *
 * This file exists because the first assistant-ui integration rendered, threw,
 * and left a blank page — the app "flashed and disappeared" on login. There was
 * no test that mounted the component at all, so nothing could have caught it.
 * A render test is the cheapest thing that would have.
 */

import { render, screen, waitFor } from "@testing-library/react";
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

  it("renders a message that carries reasoning", async () => {
    // The part most likely to be wired wrong, because it is the only one that
    // needs a part component of its own.
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
    expect(screen.getByText(/two and two/)).toBeInTheDocument();
  });

  it("offers the models it was given", async () => {
    serve(conversation([]));
    render(<Chat conversationId="c1" models={MODELS} onTurnComplete={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Model")).toBeInTheDocument());
  });
});
