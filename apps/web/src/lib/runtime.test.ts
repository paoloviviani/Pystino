/**
 * Converting our stored messages into what assistant-ui renders.
 *
 * The conversion is the entire seam between our state and the library, so the
 * things worth pinning are the ones that would be invisible if they broke:
 * reasoning arriving as its own part rather than glued to the answer, and a
 * message's status mapping onto the right terminal state.
 */

import { describe, expect, it } from "vitest";

import type { Message } from "./api";
import { toThreadMessage } from "./runtime";

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    position: 0,
    role: "assistant",
    content: "the answer",
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

describe("toThreadMessage", () => {
  it("keeps reasoning as its own part, ahead of the answer", () => {
    const converted = toThreadMessage(message({ reasoning: "let me think" }));
    const parts = converted.content as readonly { type: string; text: string }[];
    expect(parts.map((part) => part.type)).toEqual(["reasoning", "text"]);
    // Not concatenated into the prose: that is what lets it collapse, and what
    // keeps it out of the text a reader copies.
    expect(parts[1]?.text).toBe("the answer");
  });

  it("omits the reasoning part when there is none", () => {
    const parts = toThreadMessage(message()).content as readonly { type: string }[];
    expect(parts.map((part) => part.type)).toEqual(["text"]);
  });

  it("gives an empty message a part anyway", () => {
    // A message with no parts renders as nothing, and a turn that produced
    // nothing then looks like a turn that never happened.
    const parts = toThreadMessage(message({ content: "" })).content as unknown[];
    expect(parts).toHaveLength(1);
  });

  it("maps a streaming message to running, so the composer offers Stop", () => {
    expect(toThreadMessage(message({ status: "streaming" })).status).toEqual({
      type: "running",
    });
  });

  it("distinguishes cancelled from failed", () => {
    expect(toThreadMessage(message({ status: "interrupted" })).status).toEqual({
      type: "incomplete",
      reason: "cancelled",
    });
    expect(toThreadMessage(message({ status: "failed" })).status).toEqual({
      type: "incomplete",
      reason: "error",
    });
  });
});
