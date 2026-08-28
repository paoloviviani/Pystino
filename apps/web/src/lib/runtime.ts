/**
 * Bridging our conversation state to assistant-ui.
 *
 * `useExternalStoreRuntime` is the reason this library fits at all: the thread
 * state, the transport and the persistence stay ours, and assistant-ui renders
 * them. It is not an architecture we adopt, it is a view we hand our own data
 * to — which is the same rule the gateway applies to the AI SDK and to provider
 * plugins, one layer up.
 *
 * The adapter's only required member is `onNew`. Everything else here is opt-in,
 * and the ones taken are exactly the three the shell needs: send, stop, and
 * "is it running".
 */

import type { AppendMessage, ThreadMessageLike } from "@assistant-ui/react";

import type { Message } from "./api";

/**
 * Our stored message shape, as assistant-ui's.
 *
 * Reasoning becomes a **part**, not a prefix on the text: that is what lets the
 * thinking render in its own collapsible section, and — more importantly — it
 * is what keeps it out of the answer. `content` and `reasoning` are separate
 * columns server-side for the same reason.
 */
type Part = Extract<ThreadMessageLike["content"], readonly unknown[]>[number];

export function toThreadMessage(message: Message): ThreadMessageLike {
  const parts: Part[] = [];
  if (message.reasoning) {
    parts.push({ type: "reasoning", text: message.reasoning });
  }
  if (message.content) {
    parts.push({ type: "text", text: message.content });
  }
  if (parts.length === 0) {
    // A message that is genuinely empty still needs a part, or the runtime has
    // nothing to render and the turn looks like it never happened.
    parts.push({ type: "text", text: "" });
  }

  return {
    id: message.id,
    role: message.role === "system" ? "system" : message.role,
    content: parts,
    createdAt: new Date(message.created_at),
    // `running` keeps the composer in its stop state and the message marked
    // in-flight; anything else is terminal.
    status:
      message.status === "streaming"
        ? { type: "running" }
        : message.status === "failed"
          ? { type: "incomplete", reason: "error" }
          : message.status === "interrupted"
            ? { type: "incomplete", reason: "cancelled" }
            : { type: "complete", reason: "stop" },
  };
}

/** The text of a message the composer just sent. */
export function textOf(message: AppendMessage): string {
  return message.content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim();
}
