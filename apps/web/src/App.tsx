/**
 * The chat shell.
 *
 * State is deliberately local and small — a conversation list, the current
 * conversation, the person. No client-side store and no query cache yet: the
 * whole application is two resources, and the machinery would be more code than
 * the thing it manages. When assistants and knowledge bases arrive, that is the
 * moment to reconsider, not before.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Spinner } from "@llmp/ui";
import { CheckIcon, PencilIcon, XIcon } from "lucide-react";

import styles from "./App.module.css";
import { Chat } from "./routes/Chat";
import { SignIn } from "./routes/SignIn";
import {
  BASE,
  type Conversation,
  type Me,
  type Model,
  NotAuthenticatedError,
  archiveConversation,
  createConversation,
  getConversations,
  getMe,
  getModels,
  renameConversation,
  signOut,
} from "./lib/api";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bumped to re-run the load below — after a sign-in, and after the action
  // that created the first conversation ever. A load that runs once on mount
  // cannot learn that the world changed underneath it.
  const [reloadKey, setReloadKey] = useState(0);
  // A 401 no longer bounces to the identity provider: this deployment may
  // offer a password form instead, or nothing at all, and the old redirect
  // answered "Sign-in is unavailable." exactly when sign-in was available.
  // The SignIn component asks what exists and renders that.
  const [needsSignIn, setNeedsSignIn] = useState(false);

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
          setNeedsSignIn(true);
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
  }, [reloadKey]);

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

  // -- sidebar management ---------------------------------------------------
  // Rename and archive were endpoints without a UI: PATCH and DELETE existed
  // from M1's first week and nothing rendered them, so a typo'd title was
  // forever and the only way to shorten the list was the raw API. The rename
  // is inline rather than a prompt() because a native dialog cannot be
  // cancelled with Escape-and-lose-nothing, and because it would not wear
  // this app's type.

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const renameInput = useRef<HTMLInputElement>(null);

  const startRename = useCallback((conversation: Conversation) => {
    setRenamingId(conversation.id);
    // The value is in the input by the next render; focus then, not now.
    requestAnimationFrame(() => renameInput.current?.select());
  }, []);

  const commitRename = useCallback(
    async (id: string, title: string) => {
      setRenamingId(null);
      const trimmed = title.trim();
      if (!trimmed) return;
      try {
        const updated = await renameConversation(id, trimmed);
        setConversations((current) =>
          current.map((row) => (row.id === id ? { ...row, title: updated.title } : row)),
        );
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not rename.");
      }
    },
    [],
  );

  const archive = useCallback(async (id: string) => {
    try {
      await archiveConversation(id);
      setConversations((current) => {
        const next = current.filter((row) => row.id !== id);
        // The archived conversation's transcript is the thing on screen; if
        // it was, the view must follow the list — an empty main pane reads as
        // a crash even when the archive succeeded.
        setCurrentId((currentId) => (currentId === id ? (next[0]?.id ?? null) : currentId));
        return next;
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not archive.");
    }
  }, []);

  if (loading) {
    return (
      <div className={styles.shell}>
        <Spinner label="Loading" />
      </div>
    );
  }

  if (needsSignIn) {
    // Full-bleed, deliberately not inside the shell: the shell is the
    // sidebar-plus-thread grid, and the sign-in card rendered inside it
    // landed in the 17rem column beside a blank page — the grid has exactly
    // one job and no session yet means none of it applies.
    return (
      <SignIn
        onSignedIn={() => {
          setNeedsSignIn(false);
          setLoading(true);
          setReloadKey((key) => key + 1);
        }}
      />
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
          {conversations.map((conversation) =>
            conversation.id === renamingId ? (
              <form
                key={conversation.id}
                className={styles.renameRow}
                onSubmit={(event) => {
                  event.preventDefault();
                  const value = renameInput.current?.value ?? "";
                  void commitRename(conversation.id, value);
                }}
              >
                <input
                  ref={renameInput}
                  className={styles.renameInput}
                  defaultValue={conversation.title}
                  aria-label="Conversation title"
                  required
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setRenamingId(null);
                  }}
                />
                <button
                  type="submit"
                  className={styles.rowAction}
                  aria-label="Save title"
                >
                  <CheckIcon />
                </button>
                <button
                  type="button"
                  className={styles.rowAction}
                  aria-label="Cancel rename"
                  onClick={() => setRenamingId(null)}
                >
                  <XIcon />
                </button>
              </form>
            ) : (
              <div
                key={conversation.id}
                className={`${styles.row} ${
                  conversation.id === currentId ? styles.current : ""
                }`}
              >
                <button
                  type="button"
                  className={styles.conversation}
                  aria-current={conversation.id === currentId ? "true" : undefined}
                  onClick={() => setCurrentId(conversation.id)}
                >
                  {conversation.title}
                </button>
                <span className={styles.rowActions}>
                  <button
                    type="button"
                    className={styles.rowAction}
                    aria-label={`Rename "${conversation.title}"`}
                    onClick={() => startRename(conversation)}
                  >
                    <PencilIcon />
                  </button>
                  <button
                    type="button"
                    className={styles.rowAction}
                    aria-label={`Archive "${conversation.title}"`}
                    onClick={() => void archive(conversation.id)}
                  >
                    <XIcon />
                  </button>
                </span>
              </div>
            ),
          )}
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
