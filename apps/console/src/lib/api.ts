/**
 * The gateway API, from the browser.
 *
 * Same-origin, so the OIDC session cookie the gateway already sets authenticates
 * every call (ADR 0023). There is no token in JavaScript, nothing in
 * localStorage, and no second auth path — which is the main reason the console
 * is served by the gateway rather than deployed beside it.
 */

/** The gateway's error envelope, shared with the `/v1` surface. */
export interface ApiErrorBody {
  error?: {
    message?: string;
    type?: string;
    code?: string | null;
    param?: string | null;
  };
}

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

/** Raised on 401 so the app can send the reader to log in rather than show an error. */
export class NotAuthenticatedError extends ApiError {
  constructor(message = "Your session has expired.") {
    super(401, message);
    this.name = "NotAuthenticatedError";
  }
}

export class ForbiddenError extends ApiError {
  constructor(message = "You do not have access to this.") {
    super(403, message);
    this.name = "ForbiddenError";
  }
}

async function readError(response: Response): Promise<ApiError> {
  let message = `${response.status} ${response.statusText}`;
  let code: string | null = null;
  try {
    const body = (await response.json()) as ApiErrorBody;
    // The gateway writes a human-readable sentence into `message` and expects it
    // to be shown. Replacing it with our own wording here would lose the useful
    // half — "a rule already exists for that scope, metric and window" tells an
    // operator what to do next; "Conflict" does not.
    if (body.error?.message) message = body.error.message;
    code = body.error?.code ?? null;
  } catch {
    // A non-JSON body (a proxy error page, say). The status line stands.
  }

  if (response.status === 401) return new NotAuthenticatedError(message);
  if (response.status === 403) return new ForbiddenError(message);
  return new ApiError(response.status, message, code);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal } = options;

  const response = await fetch(path, {
    method,
    // Explicit rather than relying on the default: this is the line that makes
    // the session cookie travel, and it is not obvious enough to leave implied.
    credentials: "same-origin",
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Sends the browser to the gateway's OIDC login, returning to *this page*
 * afterwards.
 *
 * Without the `next`, an expired session on a deep link costs the reader their
 * place: they sign in and arrive at the overview, having asked for a quota
 * rule. The gateway validates the path and ignores anything that is not one on
 * its own origin, so this cannot become an open redirect.
 */
export function login(next?: string, provider?: string): void {
  const here = next ?? window.location.pathname + window.location.search;
  const chosen = provider ? `&provider=${encodeURIComponent(provider)}` : "";
  window.location.assign(`/auth/login?next=${encodeURIComponent(here)}${chosen}`);
}

/**
 * `next` as the router wants it.
 *
 * The stored return path is an *origin* path — `/console/admin/quotas` —
 * because the SSO round-trip hands it to the gateway, which redirects the
 * whole browser. The local sign-in has no round-trip: it calls the router's
 * `navigate`, whose basename is already `/console`, so handing it the origin
 * path produces `/console/console` — the catch-all's "No such address",
 * reached directly after a successful sign-in. Stripping the prefix is the
 * difference between the two consumers.
 */
export function nextForRouter(next: string): string {
  return next.replace(/^\/console(?=\/|$)/, "") || "/";
}

/**
 * A `next` this page may send the browser to, or null when it is not obviously
 * ours.
 *
 * Needed the moment a local sign-in can end in `window.location.assign` rather
 * than a router navigation: a full navigation follows the value verbatim, so a
 * permissive check here is an open redirect — an attacker mails
 * `/console/login?next=//evil.test`, the victim signs in for real, and lands
 * on a page of the attacker's choosing *holding a fresh session cookie*, which
 * is exactly the shape the gateway's own `_safe_next` documents. Same rule
 * here, for the same reason: a path on this origin and nothing else, rejected
 * rather than repaired — `//host` and `/\host` are protocol-relative URLs that
 * browsers resolve to another host despite the leading slash, and a value we
 * had to fix is a value we did not understand.
 */
export function safeNextPath(next: string): string | null {
  if (!next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return null;
  // Anything outside printable ASCII — CR, LF, tabs, control characters that
  // some parsers strip before resolving — is not a path anybody typed.
  if (/[^\x20-\x7e]/.test(next)) return null;
  return next;
}

/** Per-provider login, named for the button that calls it (ADR 0051). */
export function loginWith(provider: string, next?: string): void {
  login(next, provider);
}

/** Which sign-in methods the deployment offers (GET /auth/methods). */
export interface AuthMethods {
  local: boolean;
  oidc: boolean;
  /** One entry per enabled identity provider (ADR 0051): a button each. */
  providers: { name: string; issuer: string }[];
}

/**
 * Asked before any login page is rendered.
 *
 * Unauthenticated by design: the console needs the answer to decide *how* to
 * ask for credentials, which is exactly the moment it has no session. What it
 * returns — which methods are on — is already public to anyone who visits
 * /auth/login.
 */
export async function fetchAuthMethods(): Promise<AuthMethods> {
  return request<AuthMethods>("/auth/methods");
}

/**
 * Local email + password sign-in (ADR 0043).
 *
 * A `fetch`, not a navigation: the gateway sets the session cookie on the
 * response, so no page load is needed between success and a working session.
 * 401 means wrong credentials, 429 means throttled — the caller renders the
 * gateway's own message either way.
 */
export async function localLogin(email: string, password: string): Promise<void> {
  await request<{ status: string }>("/auth/login", {
    method: "POST",
    body: { email, password },
  });
}

/**
 * Triggers a browser download of a CSV endpoint.
 *
 * A plain navigation rather than fetch-and-blob: the response is a file with a
 * `content-disposition` filename the gateway chose, and letting the browser
 * handle it keeps that filename instead of inventing one here.
 */
export function downloadCsv(path: string): void {
  window.location.assign(path);
}
