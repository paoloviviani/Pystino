/**
 * One conversation, rendered by assistant-ui over our own state.
 *
 * What the library supplies: the thread viewport and its auto-scroll, the
 * composer, streaming-aware markdown with code blocks, and the collapsible
 * chain-of-thought section. What stays ours: the messages, the transport, the
 * persistence, and every decision about them — `useExternalStoreRuntime` is the
 * seam that makes that division real rather than a promise.
 *
 * The streaming assistant message lives in component state until the turn ends.
 * Writing each delta into the persisted list would re-render the whole
 * transcript per token.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  useMessagePartReasoning,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { Notice, Spinner } from "@llmp/ui";

import styles from "./Chat.module.css";
import { type Message, type Model, getConversation } from "../lib/api";
import { textOf, toThreadMessage } from "../lib/runtime";
import { streamTurn } from "../lib/stream";

interface ChatProps {
  conversationId: string;
  models: Model[];
  onTurnComplete: () => void;
}

/** A message body: markdown, with code blocks. */
function MarkdownText() {
  return <MarkdownTextPrimitive className={styles.markdown} />;
}

/**
 * The model's thinking, collapsed.
 *
 * A native `<details>`, not assistant-ui's `ChainOfThoughtPrimitive`. The
 * primitive belongs to a chain-of-thought *scope* that a plain reasoning part
 * does not establish, and using it here threw "The current scope does not have
 * a chainOfThought property" during render — one of the two crashes that made
 * this screen appear and vanish.
 *
 * `<details>` is also simply the right element: the browser supplies the
 * toggling, the keyboard behaviour and the correct role, and it degrades to
 * open text with no JavaScript at all.
 *
 * Closed by default. Reasoning is usually longer than the answer, and a
 * transcript that opens with a wall of it buries the thing the reader came for.
 * The summary says how much there is, so opening it is a choice rather than a
 * gamble.
 */
function Reasoning() {
  const reasoning = useMessagePartReasoning();
  const text = reasoning?.text ?? "";
  if (!text) return null;
  const words = text.trim().split(/\s+/).length;
  return (
    <details className={styles.thinking}>
      <summary className={styles.thinkingTrigger}>
        Thinking · {words} {words === 1 ? "word" : "words"}
      </summary>
      <div className={styles.thinkingBody}>{text}</div>
    </details>
  );
}

export function Chat({ conversationId, models, onTurnComplete }: ChatProps) {
  const [title, setTitle] = useState("");
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void getConversation(conversationId)
      .then((conversation) => {
        if (cancelled) return;
        setTitle(conversation.title);
        setModel(conversation.model);
        setMessages(conversation.messages);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not load.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      // Leaving the conversation stops the turn. The server keeps what was
      // generated; the gateway has already billed it.
      abort.current?.abort();
    };
  }, [conversationId]);

  const send = useCallback(
    async (text: string) => {
      if (!text) return;
      setError(null);
      setRunning(true);

      const controller = new AbortController();
      abort.current = controller;
      const now = new Date().toISOString();
      const blank = (role: Message["role"], content: string): Message => ({
        id: `local-${role}-${now}`,
        position: 0,
        role,
        content,
        reasoning: null,
        status: role === "assistant" ? "streaming" : "complete",
        model: null,
        request_id: null,
        error: null,
        usage: null,
        created_at: now,
      });

      setMessages((current) => [...current, blank("user", text), blank("assistant", "")]);

      const patchLast = (change: (message: Message) => Message) =>
        setMessages((current) =>
          current.map((message, index) =>
            index === current.length - 1 ? change(message) : message,
          ),
        );

      await streamTurn(
        conversationId,
        { content: text, model },
        {
          onDelta: (piece) =>
            patchLast((message) => ({ ...message, content: message.content + piece })),
          onReasoning: (piece) =>
            patchLast((message) => ({
              ...message,
              reasoning: (message.reasoning ?? "") + piece,
            })),
          onError: (message) => {
            setError(message);
            patchLast((last) => ({ ...last, status: "failed", error: message }));
          },
          onDone: () => patchLast((message) => ({ ...message, status: "complete" })),
        },
        controller.signal,
      );

      // The stop button leaves the message mid-flight; the server has already
      // marked its row interrupted, so match it rather than claim completion.
      if (controller.signal.aborted) {
        patchLast((message) => ({ ...message, status: "interrupted" }));
      }
      setRunning(false);
      abort.current = null;
      onTurnComplete();
    },
    [conversationId, model, onTurnComplete],
  );

  const runtime = useExternalStoreRuntime({
    isRunning: running,
    // Our rows, converted on the way in. The adapter exists so the store stays
    // ours; handing it pre-converted messages would put a second copy of the
    // transcript in play.
    messages,
    convertMessage: toThreadMessage,
    onNew: async (message) => {
      await send(textOf(message));
    },
    onCancel: async () => {
      abort.current?.abort();
    },
  });

  if (loading) return <Spinner label="Loading conversation" />;

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <header className={styles.header}>
        <span className={styles.title}>{title}</span>
        <select
          className={styles.model}
          aria-label="Model"
          value={model}
          onChange={(event) => setModel(event.target.value)}
        >
          {models.map((available) => (
            <option key={available.id} value={available.id}>
              {available.id}
            </option>
          ))}
        </select>
      </header>

      <ThreadPrimitive.Root className={styles.thread}>
        <ThreadPrimitive.Viewport className={styles.transcript} autoScroll>
          <ThreadPrimitive.Messages
            components={{
              UserMessage: () => (
                <article className={styles.turn}>
                  <span className={styles.who}>You</span>
                  <MessagePrimitive.Root className={styles.body}>
                    <MessagePrimitive.Parts />
                  </MessagePrimitive.Root>
                </article>
              ),
              AssistantMessage: () => (
                <article className={styles.turn}>
                  <span className={styles.who}>Assistant</span>
                  <MessagePrimitive.Root className={styles.body}>
                    <MessagePrimitive.Parts
                      components={{ Text: MarkdownText, Reasoning }}
                    />
                  </MessagePrimitive.Root>
                </article>
              ),
            }}
          />
          {error ? (
            // The gateway's own words. "You have exceeded your monthly budget"
            // is something the reader can act on; "something went wrong" is not.
            <Notice tone="danger" title="The message was not answered">
              {error}
            </Notice>
          ) : null}
        </ThreadPrimitive.Viewport>

        <ComposerPrimitive.Root className={styles.composer}>
          <ComposerPrimitive.Input
            className={styles.input}
            placeholder="Message"
            aria-label="Message"
            rows={1}
            autoFocus
          />
          <ThreadPrimitive.If running={false}>
            <ComposerPrimitive.Send className={styles.send}>Send</ComposerPrimitive.Send>
          </ThreadPrimitive.If>
          <ThreadPrimitive.If running>
            {/* Only while a turn is in flight, and in the same place as Send —
                a stop button that appears elsewhere is one nobody finds. */}
            <ComposerPrimitive.Cancel className={styles.stop}>Stop</ComposerPrimitive.Cancel>
          </ThreadPrimitive.If>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
