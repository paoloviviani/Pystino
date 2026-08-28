/**
 * The model thinking out loud, collapsed.
 *
 * assistant-ui's own arrangement, kept faithfully because the class names are
 * what `@assistant-ui/styles` styles — and it is *faithfulness* that was the
 * bug: that stylesheet is compiled from Tailwind, and several of its rules only
 * fire through Tailwind's group selectors. `group/trigger` rotates the chevron,
 * `group/collapsible-content` runs the enter/exit animation on the body, and
 * the shimmer is an absolutely-positioned **duplicate** of the label inside the
 * wrapper, filled with a moving gradient via background-clip. Omit any of those
 * and the part renders — static label, dead chevron, no shimmer — which is
 * exactly how this looked before the group classes were added.
 *
 * Two behaviours that are theirs and worth not losing:
 *
 * - **Open while streaming, closed once it stops.** Thinking is interesting as
 *   it happens and noise afterwards — it is usually longer than the answer, and
 *   a transcript that stays open on it buries what the reader came for. The
 *   first manual toggle takes over permanently, so a reader who wants it open
 *   keeps it open.
 * - **The shimmer is the streaming indicator**, not a spinner. It says "still
 *   thinking" in the one place the reader is already looking.
 *
 * `useMessagePartReasoning` supplies the text; the part component is rendered
 * once per reasoning part by `MessagePrimitive.Parts`.
 */

import { useAuiState, useMessagePartReasoning } from "@assistant-ui/react";
import { BrainIcon, ChevronDownIcon } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useEffect, useRef, useState } from "react";

export function Reasoning() {
  const reasoning = useMessagePartReasoning();
  const text = reasoning?.text ?? "";

  // The *message* is running; a reasoning part that has stopped producing while
  // the answer continues is no longer the thing to shimmer about.
  const streaming = useAuiState((s) => s.message.status?.type === "running");
  const isStreaming = streaming && (reasoning?.status?.type ?? "complete") === "running";

  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? isStreaming;

  // Follow the newest tokens while it is open and streaming, the way a terminal
  // follows a log. Only while the reader has not scrolled up themselves.
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = bodyRef.current;
    if (!node || !open || !isStreaming) return;
    node.scrollTop = node.scrollHeight;
  }, [text, open, isStreaming]);

  if (!text) return null;

  return (
    <Collapsible.Root
      className="aui-reasoning-root group/reasoning-root"
      open={open}
      onOpenChange={setUserOpen}
      style={{ ["--animation-duration" as string]: "200ms" }}
    >
      <Collapsible.Trigger className="aui-reasoning-trigger group/trigger">
        <BrainIcon className="aui-reasoning-trigger-icon" aria-hidden="true" />
        <span className="aui-reasoning-trigger-label-wrapper">
          {isStreaming ? "Thinking" : "Thought"}
          {/* The shimmer overlay: the same word, absolutely positioned on top,
              clipped to the glyphs and filled with a gradient that moves. It is
              a separate element because background-clip needs text to clip —
              the plain label below it stays readable if the gradient stalls. */}
          {isStreaming && (
            <span className="aui-reasoning-trigger-shimmer" aria-hidden="true">
              Thinking
            </span>
          )}
        </span>
        <ChevronDownIcon className="aui-reasoning-trigger-chevron" aria-hidden="true" />
      </Collapsible.Trigger>
      <Collapsible.Content className="aui-reasoning-content group/collapsible-content" aria-busy={isStreaming}>
        <div className="aui-reasoning-fade" />
        <div ref={bodyRef} className="aui-reasoning-text">
          {/* Plain text, not markdown. Reasoning is a stream of thought and
              rendering it as a document gives half-written headings and lists
              that reflow on every token. */}
          <div className="aui-reasoning-text-content">{text}</div>
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
