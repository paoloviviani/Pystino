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

/** Sends the browser to the gateway's OIDC login, returning here afterwards. */
export function login(): void {
  window.location.assign("/auth/login");
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
