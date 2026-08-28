/**
 * One conversation, rendered by assistant-ui.
 *
 * **Why this looked crude before.** We used assistant-ui's headless primitives
 * and styled them with about a hundred lines of our own CSS. The primitives are
 * the behaviour — the thread runtime, the viewport's auto-scroll, the composer's
 * state, the parts model — and they carry no appearance at all. Their examples
 * ship a three-thousand-line stylesheet on top. We had their engine and none of
 * their bodywork.
 *
 * So the structure below is theirs, class names included: `@assistant-ui/styles`
 * is their own components compiled out of Tailwind and published as plain CSS
 * for projects that have none, and every rule in it is keyed to an `aui-*`
 * class. Follow the names and the thread looks like their examples; invent our
 * own and it arrives unstyled. `aui-theme.css` maps the variables that
 * stylesheet expects onto our tokens, so it wears this platform's palette
 * rather than shadcn's default slate.
 *
 * What stays ours: the store, the transport and the persistence, through
 * `useExternalStoreRuntime`. It is a view over our data, not an architecture we
 * adopted.
 */

import {
  ActionBarPrimitive,
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import { Notice, Spinner } from "@llmp/ui";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CopyIcon,
  RefreshCwIcon,
  SquareIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { MarkdownText } from "../components/aui/MarkdownText";
import { Reasoning } from "../components/aui/Reasoning";
import styles from "./Chat.module.css";
import { type Message, type Model, getConversation } from "../lib/api";
import { textOf, toThreadMessage } from "../lib/runtime";
import { streamTurn } from "../lib/stream";

interface ChatProps {
  conversationId: string;
  models: Model[];
  onTurnComplete: () => void;
}

const PART_COMPONENTS = { Text: MarkdownText, Reasoning };

/* Module level, not inline in the JSX. An inline `() => …` is a new component
   *type* on every render, and this component re-renders on every streamed
   token — React would unmount and remount the whole transcript per delta. That
   was a real bug here, seen as text flickering while an answer arrived. */
function UserMessage() {
  return (
    <MessagePrimitive.Root className="aui-user-message-root" data-role="user">
      <div className="aui-user-message-content-wrapper">
        <div className="aui-user-message-content">
          <MessagePrimitive.Parts />
        </div>
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="aui-assistant-message-root" data-role="assistant">
      <div className="aui-assistant-message-content">
        <MessagePrimitive.Parts components={PART_COMPONENTS} />
      </div>
      <ActionBarPrimitive.Root
        className="aui-assistant-action-bar-root"
        hideWhenRunning
        autohide="not-last"
      >
        <ActionBarPrimitive.Copy className="aui-button-icon" aria-label="Copy">
          <CopyIcon />
        </ActionBarPrimitive.Copy>
        <ActionBarPrimitive.Reload className="aui-button-icon" aria-label="Regenerate">
          <RefreshCwIcon />
        </ActionBarPrimitive.Reload>
      </ActionBarPrimitive.Root>
    </MessagePrimitive.Root>
  );
}

const MESSAGE_COMPONENTS = { UserMessage, AssistantMessage };

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

      // Stop leaves the message mid-flight; the server has already marked its
      // row interrupted, so match it rather than claim completion.
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

      <ThreadPrimitive.Root className="aui-root aui-thread-root">
        <ThreadPrimitive.Viewport className="aui-thread-viewport" turnAnchor="top">
          <ThreadPrimitive.Empty>
            <div className="aui-thread-welcome-root">
              <h1 className="aui-thread-welcome-message-inner">How can I help you today?</h1>
            </div>
          </ThreadPrimitive.Empty>

          <ThreadPrimitive.Messages components={MESSAGE_COMPONENTS} />

          {error ? (
            // The gateway's own words. "You have exceeded your monthly budget"
            // is something the reader can act on; "something went wrong" is not.
            <Notice tone="danger" title="The message was not answered">
              {error}
            </Notice>
          ) : null}

          <ThreadPrimitive.ViewportFooter className="aui-thread-viewport-footer">
            <ThreadPrimitive.ScrollToBottom
              className="aui-thread-scroll-to-bottom"
              aria-label="Scroll to bottom"
            >
              <ArrowDownIcon />
            </ThreadPrimitive.ScrollToBottom>

            <ComposerPrimitive.Root className="aui-composer-root">
              <div className={styles.composerShell}>
                <ComposerPrimitive.Input
                  className="aui-composer-input"
                  placeholder="Send a message…"
                  rows={1}
                  autoFocus
                  aria-label="Message"
                />
                <div className="aui-composer-action-wrapper">
                  <span />
                  <ThreadPrimitive.If running={false}>
                    <ComposerPrimitive.Send className="aui-composer-send" aria-label="Send">
                      <ArrowUpIcon className="aui-composer-send-icon" />
                    </ComposerPrimitive.Send>
                  </ThreadPrimitive.If>
                  <ThreadPrimitive.If running>
                    {/* In the same place as Send, because a stop button
                        somewhere else is one nobody finds in time. */}
                    <ComposerPrimitive.Cancel
                      className="aui-composer-cancel"
                      aria-label="Stop"
                    >
                      <SquareIcon className="aui-composer-cancel-icon" />
                    </ComposerPrimitive.Cancel>
                  </ThreadPrimitive.If>
                </div>
              </div>
            </ComposerPrimitive.Root>
          </ThreadPrimitive.ViewportFooter>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
