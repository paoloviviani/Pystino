/**
 * The chat API, from the browser.
 *
 * Same-origin, so the session cookie chat-api sets authenticates every call.
 * There is no token in JavaScript and nothing in localStorage — the access
 * token that reaches the gateway is minted server-side, per request, from a
 * refresh token the browser never sees.
 */

/**
 * Every path this application serves lives under this prefix, in development
 * and behind the proxy alike. At the root of the deployed origin, `/api` and
 * `/auth` belong to the gateway.
 */
export const BASE = "/chat";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** Raised on 401 so the app can send the reader to sign in rather than show an error. */
export class NotAuthenticatedError extends ApiError {
  constructor(message = "Your session has expired.") {
    super(401, message);
    this.name = "NotAuthenticatedError";
  }
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function messageOf(body: unknown, fallback: string): { message: string; code: string | null } {
  if (body && typeof body === "object") {
    const error = (body as { error?: unknown; detail?: unknown }).error;
    if (error && typeof error === "object") {
      const shape = error as { message?: unknown; code?: unknown };
      return {
        message: typeof shape.message === "string" ? shape.message : fallback,
        code: typeof shape.code === "string" ? shape.code : null,
      };
    }
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return { message: detail, code: null };
  }
  return { message: fallback, code: null };
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
    // Same-origin by default, stated because the whole auth model rests on it.
    credentials: "same-origin",
  });

  if (response.status === 401) throw new NotAuthenticatedError();
  const body = await parse(response);
  if (!response.ok) {
    const { message, code } = messageOf(body, `Request failed (${response.status}).`);
    throw new ApiError(response.status, message, code);
  }
  return body as T;
}

export interface Me {
  subject: string;
  email: string | null;
  display_name: string | null;
  groups: string[];
  is_admin: boolean;
  console_url: string;
}

export interface Model {
  id: string;
  owned_by: string | null;
  /** "reasoning", "tools", "json_mode" — what the gateway says it can do. */
  supported_features: string[];
}

/**
 * Can this model be asked to think aloud?
 *
 * A capability, not a name: `deepseek-v4-flash-0731` reasons and does not say
 * so in its id, while a model called "-thinking" might not be granted here at
 * all. The gateway already carries the answer on the model card.
 */
export const canThink = (model: Model | undefined): boolean =>
  model?.supported_features.includes("reasoning") ?? false;

export interface Conversation {
  id: string;
  title: string;
  model: string;
  created_at: string;
  updated_at: string;
}

export type MessageStatus = "streaming" | "complete" | "interrupted" | "failed";

export interface Message {
  id: string;
  position: number;
  role: "user" | "assistant" | "system";
  content: string;
  /** The model's own thinking, kept apart from the answer end to end. */
  reasoning: string | null;
  status: MessageStatus;
  model: string | null;
  request_id: string | null;
  error: string | null;
  usage: Record<string, unknown> | null;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export const getMe = () => api<Me>(`${BASE}/api/me`);
export const getModels = () => api<{ data: Model[] }>(`${BASE}/api/models`);
export const getConversations = () => api<{ data: Conversation[] }>(`${BASE}/api/conversations`);
export const getConversation = (id: string) => api<ConversationDetail>(`${BASE}/api/conversations/${id}`);

export const createConversation = (model: string) =>
  api<ConversationDetail>(`${BASE}/api/conversations`, {
    method: "POST",
    body: JSON.stringify({ model }),
  });

export const renameConversation = (id: string, title: string) =>
  api<Conversation>(`${BASE}/api/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });

export const archiveConversation = (id: string) =>
  api<void>(`${BASE}/api/conversations/${id}`, { method: "DELETE" });

export const signOut = () => api<void>(`${BASE}/api/auth/logout`, { method: "POST" });
