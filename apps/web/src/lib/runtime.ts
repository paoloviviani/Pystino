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

  const base = {
    id: message.id,
    role: message.role === "system" ? ("system" as const) : message.role,
    content: parts,
    createdAt: new Date(message.created_at),
  };

  // **Only assistant messages carry a status.** The converter throws
  // "status is only supported for assistant messages" otherwise — and it runs
  // on every snapshot, so setting it on the user's turn crashed the whole
  // thread the moment a conversation with any history loaded. That is the bug
  // that made the app flash and vanish on sign-in.
  if (message.role !== "assistant") return base;

  return {
    ...base,
    // The footer under an answer reads these back out of the runtime state:
    // which model actually served it and what it cost in tokens. Passed as
    // `custom` because that is the one field the converter preserves verbatim.
    metadata: { custom: { model: message.model, usage: message.usage } },
    // `running` keeps the composer in its stop state and the message marked
    // in-flight; anything else is terminal.
    status:
      message.status === "streaming"
        ? { type: "running" as const }
        : message.status === "failed"
          ? { type: "incomplete" as const, reason: "error" as const }
          : message.status === "interrupted"
            ? { type: "incomplete" as const, reason: "cancelled" as const }
            : { type: "complete" as const, reason: "stop" as const },
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
