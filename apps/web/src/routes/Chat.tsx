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
  useAuiState,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import { Notice, Spinner } from "@llmp/ui";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  CopyIcon,
  PencilIcon,
  RefreshCwIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { MarkdownText } from "../components/aui/MarkdownText";
import { Reasoning } from "../components/aui/Reasoning";
import styles from "./Chat.module.css";
import {
  type Message,
  type Model,
  canThink,
  deleteFromMessage,
  getConversation,
} from "../lib/api";
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
        <ComposerPrimitive.If editing={false}>
          <div className="aui-user-message-content">
            <MessagePrimitive.Parts />
          </div>
        </ComposerPrimitive.If>
        {/* Hovering the bubble reveals copy and edit, floated into the
            gutter: the actions live beside what they act on, where a reader
            is already pointing, and they stay out of the transcript itself. */}
        <ComposerPrimitive.If editing={false}>
          <div className="aui-user-action-bar-wrapper">
            <ActionBarPrimitive.Root hideWhenRunning autohide="not-last" className="aui-user-action-bar-root">
              <UserCopyButton />
              <ActionBarPrimitive.Edit className="aui-button-icon" aria-label="Edit">
                <PencilIcon />
              </ActionBarPrimitive.Edit>
            </ActionBarPrimitive.Root>
          </div>
        </ComposerPrimitive.If>
        <ComposerPrimitive.If editing>
          <ComposerPrimitive.Root className="aui-edit-composer-root">
            <ComposerPrimitive.Input className="aui-edit-composer-input" autoFocus aria-label="Edit message" />
            <div className="aui-edit-composer-footer">
              <ComposerPrimitive.Cancel className="aui-button-icon" aria-label="Cancel edit">
                <XIcon />
              </ComposerPrimitive.Cancel>
              <ComposerPrimitive.Send className="aui-button-icon" aria-label="Resend">
                <ArrowUpIcon />
              </ComposerPrimitive.Send>
            </div>
          </ComposerPrimitive.Root>
        </ComposerPrimitive.If>
      </div>
    </MessagePrimitive.Root>
  );
}

/** Copy with an acknowledgement, because a button that flashes nothing leaves
    the reader guessing whether the press landed. */
function UserCopyButton() {
  const [copied, setCopied] = useState(false);
  return (
    <ActionBarPrimitive.Copy
      className="aui-button-icon"
      aria-label="Copy"
      onClick={() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </ActionBarPrimitive.Copy>
  );
}

/** Is the turn running but nothing readable has arrived yet? */
function useWaitingForFirstToken(): boolean {
  const running = useAuiState((s) => s.message.status?.type === "running");
  const silent = useAuiState((s) =>
    s.message.content.every((part) => {
      if (part.type === "text" || part.type === "reasoning") return part.text.length === 0;
      return false;
    }),
  );
  return Boolean(running && silent);
}

/** Which model served this, and what it cost in tokens.
 *
 * We already record both per message; the footer is where they become
 * visible to the person the bill names. Read back out of the runtime state
 * through `metadata.custom`, which is the one field the message converter
 * preserves verbatim.
 */
function AssistantFooter() {
  const status = useAuiState((s) => s.message.status?.type);
  const custom = useAuiState((s) => s.message.metadata?.custom) as
    | { model?: string | null; usage?: { total_tokens?: number } | null }
    | undefined;
  if (status !== "complete" || !custom) return null;
  const tokens = custom.usage?.total_tokens;
  const parts = [custom.model ?? null, tokens != null ? `${tokens.toLocaleString()} tokens` : null];
  const shown = parts.filter(Boolean);
  return shown.length > 0 ? (
    <div className="aui-assistant-message-footer">
      {shown.map((part) => (
        <span key={part} className={styles.footerItem}>
          {part}
        </span>
      ))}
    </div>
  ) : null;
}

function AssistantMessage() {
  const waiting = useWaitingForFirstToken();
  return (
    <MessagePrimitive.Root className="aui-assistant-message-root" data-role="assistant">
      {/* The gap between send and first token is otherwise dead air: a blank
          bubble for however long the model spends before it writes. Three
          breathing dots say the turn is alive — the reasoning disclosure takes
          over the moment thinking tokens actually arrive. */}
      {waiting && (
        <div className={styles.thinking} role="status" aria-label="Thinking">
          <span />
          <span />
          <span />
        </div>
      )}
      <div className="aui-assistant-message-content">
        <MessagePrimitive.Parts components={PART_COMPONENTS} />
      </div>
      <AssistantFooter />
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

/** Where the answer just ended, the obvious next questions, one click each.
 *
 * Deliberately *transforms of the previous answer* rather than a model's guess
 * at what to ask next: generating suggestions would cost a billed request per
 * turn and arrive late, and these three are always applicable to whatever the
 * model just said. Hidden while a turn runs, and in an empty thread — the
 * welcome suggestions speak there instead.
 */
const FOLLOWUPS = ["Go deeper on that.", "Show an example.", "Summarise in three bullets."];

function Followups({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="aui-thread-followup-suggestions">
      {FOLLOWUPS.map((prompt) => (
        <button
          key={prompt}
          type="button"
          className="aui-thread-followup-suggestion"
          onClick={() => onPick(prompt)}
        >
          {prompt}
        </button>
      ))}
    </div>
  );
}

/** What an empty thread offers. Clicking one starts the turn — the same path
    as typing, so there is exactly one way a message enters the transcript. */
const SUGGESTIONS: readonly [string, string][] = [
  ["Explain", "how this gateway meters and bills a request"],
  ["Draft", "a short update for my team"],
  ["Summarise", "the trade-offs of caching prompts"],
  ["Brainstorm", "names for a side project"],
];

function Welcome({ onSuggest }: { onSuggest: (text: string) => void }) {
  return (
    <div className="aui-thread-welcome-root">
      <div className="aui-thread-welcome-center">
        <div className="aui-thread-welcome-message">
          <h1 className="aui-thread-welcome-message-inner">How can I help you today?</h1>
        </div>
        <div className="aui-thread-welcome-suggestions">
          {SUGGESTIONS.map(([lead, rest]) => (
            <button
              key={lead}
              type="button"
              className="aui-thread-welcome-suggestion"
              onClick={() => onSuggest(`${lead} ${rest}`)}
            >
              <span className="aui-thread-welcome-suggestion-display">
                <span className="aui-thread-welcome-suggestion-text-1">{lead}</span>
              </span>
              <span className="aui-thread-welcome-suggestion-display">
                <span className="aui-thread-welcome-suggestion-text-2">&ldquo;{rest}&rdquo;</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
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
  // The edit and reload adapters need the transcript as of the click, not as
  // of the render they were defined in; event handlers read between renders,
  // so assigning during render keeps this always current.
  const messagesRef = useRef<Message[]>([]);
  messagesRef.current = messages;

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

  /** Stream one assistant reply, optionally behind a new user message.
   *
   * `userText === null` is a regeneration: no user row locally either — the
   * caller has already removed the attempt being replaced, and the server
   * writes no user row for a contentless turn.
   */
  const runTurn = useCallback(
    async (userText: string | null, replaceFrom: number | null) => {
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

      if (replaceFrom !== null) {
        // The attempt being replaced disappears from the view at the same
        // moment the server row does — the transcript never shows a reply
        // that is already gone upstream.
        setMessages((current) => current.slice(0, replaceFrom));
      }
      setMessages((current) => [
        ...current,
        ...(userText !== null ? [blank("user", userText)] : []),
        blank("assistant", ""),
      ]);

      const patchLast = (change: (message: Message) => Message) =>
        setMessages((current) =>
          current.map((message, index) =>
            index === current.length - 1 ? change(message) : message,
          ),
        );

      await streamTurn(
        conversationId,
        // Ask for thinking whenever the model can do it. Not a toggle in the
        // UI: a reasoning model that silently stops reasoning because a setting
        // was off is a worse surprise than one that always shows its work, and
        // the disclosure is collapsed by default anyway.
        { content: userText, model, thinking: canThink(models.find((m) => m.id === model)) },
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
    [conversationId, model, models, onTurnComplete],
  );

  const send = useCallback(
    async (text: string) => {
      if (!text) return;
      await runTurn(text, null);
    },
    [runTurn],
  );

  /** The index of the message with this id, or -1 when it is not ours to find.
   * Local (in-flight) ids are never truncated: the server does not know them. */
  const indexOf = useCallback(
    (id: string | null | undefined, current: Message[]): number =>
      id == null ? -1 : current.findIndex((message) => message.id === id),
    [],
  );

  /** Cut the transcript from `from` onward, server first.
   * If the server refuses, the local view keeps what the server kept: the two
   * must not diverge over a failure the reader can see and retry. */
  const truncateFrom = useCallback(
    async (from: number): Promise<boolean> => {
      const target = messagesRef.current[from];
      if (!target) return false;
      // A message still being written has no server row; the stop button is
      // the tool for that case, not the edit one.
      if (target.id.startsWith("local-")) return true;
      try {
        await deleteFromMessage(conversationId, target.id);
        return true;
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not delete.");
        return false;
      }
    },
    [conversationId],
  );

  const runtime = useExternalStoreRuntime({
    isRunning: running,
    messages,
    convertMessage: toThreadMessage,
    onNew: async (message) => {
      await send(textOf(message));
    },
    onEdit: async (message) => {
      // `parentId` is the message *before* the one being edited (null when it
      // is the first), so the edited message is one past it — and everything
      // from there answered a prompt that no longer exists.
      const parent = indexOf(message.parentId, messagesRef.current);
      const editedAt = parent + 1;
      if (!(await truncateFrom(editedAt))) return;
      await send(textOf(message));
    },
    onReload: async (parentId) => {
      // Reload sits on an assistant message; `parentId` is the prompt before
      // it, and the reply after that prompt is what gets replaced.
      const parent = indexOf(parentId, messagesRef.current);
      const replyAt = parent + 1;
      if (!(await truncateFrom(replyAt))) return;
      await runTurn(null, replyAt);
    },
    onCancel: async () => {
      abort.current?.abort();
    },
  });

  const empty = messages.length === 0;

  if (loading) return <Spinner label="Loading conversation" />;

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <header className={styles.header}>
        <span className={styles.title}>{title || "New conversation"}</span>
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

      {/* `aui-thread-root` only — **not** `aui-root`, which their own JSX also
          carries. In a Tailwind project the utilities beside it win; in the
          compiled standalone stylesheet `.aui-root` is the *floating modal*
          assistant: `position: fixed; right: 1rem; bottom: 1rem; width: 400px;
          height: 500px`. Copying the class list wholesale pinned the entire
          thread into a 400px box in the bottom-right corner. */}
      <ThreadPrimitive.Root className={`aui-thread-root ${styles.thread}`}>
        <ThreadPrimitive.Viewport className="aui-thread-viewport" turnAnchor="top">
          {/* Their own structure: the viewport scrolls, and a column inside it
              holds the reading measure. Messages centre themselves within
              whatever contains them, so without this they centre in the
              viewport's full width. */}
          <div className={`${styles.threadColumn} ${empty ? styles.threadColumnEmpty : ""}`}>
            <ThreadPrimitive.Empty>
              <Welcome onSuggest={(text) => void send(text)} />
            </ThreadPrimitive.Empty>

            <div className={styles.messages}>
              <ThreadPrimitive.Messages components={MESSAGE_COMPONENTS} />
            </div>

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

            {running || error || messages.at(-1)?.role !== "assistant" ? null : (
              <Followups onPick={(text) => void send(text)} />
            )}

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
          </div>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
