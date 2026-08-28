/**
 * One conversation: its transcript, and the box you type in.
 *
 * The streaming assistant message is component state rather than a row in the
 * list, and only becomes a message when the turn ends. Mutating a persisted
 * message on every delta would re-render the whole transcript per token, which
 * is a hundred renders for a short answer.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Notice, Select, Spinner } from "@llmp/ui";

import styles from "./Chat.module.css";
import { type Message, type Model, getConversation } from "../lib/api";
import { streamTurn } from "../lib/stream";

interface ChatProps {
  conversationId: string;
  models: Model[];
  onTurnComplete: () => void;
}

export function Chat({ conversationId, models, onTurnComplete }: ChatProps) {
  const [title, setTitle] = useState("");
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const transcriptRef = useRef<HTMLDivElement>(null);

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
    };
  }, [conversationId]);

  // Follow the bottom while an answer arrives. Not conditional on the reader
  // having scrolled up yet — that is a real refinement and a separate one.
  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, pending]);

  const send = useCallback(async () => {
    const content = draft.trim();
    if (!content || pending !== null) return;

    setDraft("");
    setError(null);
    setPending("");
    setMessages((current) => [
      ...current,
      {
        id: `local-${current.length}`,
        position: current.length,
        role: "user",
        content,
        status: "complete",
        model: null,
        request_id: null,
        error: null,
        usage: null,
        created_at: new Date().toISOString(),
      },
    ]);

    let failed = false;
    await streamTurn(
      conversationId,
      { content, model },
      {
        onDelta: (piece) => setPending((current) => (current ?? "") + piece),
        onError: (message) => {
          failed = true;
          setError(message);
        },
        onDone: () => undefined,
      },
    );

    setPending((finished) => {
      if (finished && !failed) {
        setMessages((current) => [
          ...current,
          {
            id: `local-assistant-${current.length}`,
            position: current.length,
            role: "assistant",
            content: finished,
            status: "complete",
            model,
            request_id: null,
            error: null,
            usage: null,
            created_at: new Date().toISOString(),
          },
        ]);
      }
      return null;
    });
    onTurnComplete();
  }, [conversationId, draft, model, pending, onTurnComplete]);

  if (loading) return <Spinner label="Loading conversation" />;

  return (
    <>
      <header className={styles.header}>
        <span className={styles.title}>{title}</span>
        <Select
          label="Model"
          hideLabel
          value={model}
          onChange={(event) => setModel(event.target.value)}
        >
          {models.map((available) => (
            <option key={available.id} value={available.id}>
              {available.id}
            </option>
          ))}
        </Select>
      </header>

      <div className={styles.transcript} ref={transcriptRef}>
        {messages.map((message) => (
          <article key={message.id} className={styles.turn}>
            <span className={styles.who}>{message.role === "user" ? "You" : "Assistant"}</span>
            <div className={styles.body}>{message.content}</div>
          </article>
        ))}

        {pending !== null ? (
          <article className={styles.turn}>
            <span className={styles.who}>Assistant</span>
            <div className={`${styles.body} ${pending === "" ? styles.pending : ""}`}>
              {pending === "" ? "…" : pending}
            </div>
          </article>
        ) : null}

        {error ? (
          // The gateway's own words. "You have exceeded your monthly budget" is
          // something the reader can act on; "something went wrong" is not.
          <Notice tone="danger" title="The message was not answered">
            {error}
          </Notice>
        ) : null}
      </div>

      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          className={styles.input}
          value={draft}
          placeholder="Message"
          aria-label="Message"
          rows={1}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter breaks the line — the convention every
            // chat client shares, and getting it backwards is immediately felt.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <Button type="submit" disabled={pending !== null || draft.trim() === ""}>
          Send
        </Button>
      </form>
    </>
  );
}
