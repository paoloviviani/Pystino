/**
 * The chat shell.
 *
 * State is deliberately local and small — a conversation list, the current
 * conversation, the person. No client-side store and no query cache yet: the
 * whole application is two resources, and the machinery would be more code than
 * the thing it manages. When assistants and knowledge bases arrive, that is the
 * moment to reconsider, not before.
 */

import { useCallback, useEffect, useState } from "react";
import { Button, Spinner } from "@llmp/ui";

import styles from "./App.module.css";
import { Chat } from "./routes/Chat";
import {
  BASE,
  type Conversation,
  type Me,
  type Model,
  NotAuthenticatedError,
  createConversation,
  getConversations,
  getMe,
  getModels,
  signOut,
} from "./lib/api";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [profile, catalogue, list] = await Promise.all([
          getMe(),
          getModels(),
          getConversations(),
        ]);
        if (cancelled) return;
        setMe(profile);
        setModels(catalogue.data);
        setConversations(list.data);
        setCurrentId(list.data[0]?.id ?? null);
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof NotAuthenticatedError) {
          // Straight to the identity provider. An "you are signed out" screen
          // with a button on it is one click nobody wants to make.
          window.location.href = `${BASE}/auth/login?next=${encodeURIComponent(
            window.location.pathname,
          )}`;
          return;
        }
        setError(caught instanceof Error ? caught.message : "Could not load.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshList = useCallback(async () => {
    const list = await getConversations();
    setConversations(list.data);
  }, []);

  const startNew = useCallback(async () => {
    const model = models[0]?.id;
    if (!model) return;
    const conversation = await createConversation(model);
    setConversations((current) => [conversation, ...current]);
    setCurrentId(conversation.id);
  }, [models]);

  if (loading) {
    return (
      <div className={styles.shell}>
        <Spinner label="Loading" />
      </div>
    );
  }

  if (error) {
    return <p role="alert">{error}</p>;
  }

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <span className={styles.brandName}>Chat</span>
          <Button variant="secondary" onClick={() => void startNew()} disabled={models.length === 0}>
            New
          </Button>
        </div>

        <nav className={styles.conversations} aria-label="Conversations">
          {conversations.length === 0 && (
            <span className={styles.emptyHint}>No conversations yet.</span>
          )}
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={`${styles.conversation} ${
                conversation.id === currentId ? styles.current : ""
              }`}
              aria-current={conversation.id === currentId ? "true" : undefined}
              onClick={() => setCurrentId(conversation.id)}
            >
              {conversation.title}
            </button>
          ))}
        </nav>

        <div className={styles.footer}>
          <span className={styles.identity}>{me?.email ?? me?.subject}</span>
          {me?.is_admin && me.console_url ? (
            // A link, not an embedded screen. Providers, quotas, redaction and
            // spend belong to the console; rendering them here would mean
            // testing every console change in two hosts.
            <a href={me.console_url}>Gateway console</a>
          ) : null}
          <Button
            variant="ghost"
            onClick={() => {
              void signOut().finally(() => {
                window.location.href = `${BASE}/`;
              });
            }}
          >
            Sign out
          </Button>
        </div>
      </aside>

      <main className={styles.main}>
        {currentId ? (
          <Chat
            key={currentId}
            conversationId={currentId}
            models={models}
            onTurnComplete={() => void refreshList()}
          />
        ) : (
          <p className={styles.empty}>
            {models.length === 0
              ? "No models are available to you yet. An administrator grants access in the gateway console."
              : "Start a conversation."}
          </p>
        )}
      </main>
    </div>
  );
}
