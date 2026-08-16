/**
 * Shared test plumbing.
 *
 * Only what more than one test file needs, and only where getting it wrong in
 * one file would make that file agree with a screen that is broken.
 */

/**
 * A JSON response in the shape the gateway actually sends.
 *
 * Listing endpoints answer with a pagination envelope. Fixtures stay as plain
 * arrays — that is what a test is about — and are wrapped here, so the envelope
 * is described once rather than copied into every mock. A non-array payload is
 * passed through untouched, which is what a single-object route returns.
 */
export function jsonResponse(payload: unknown, status = 200): Response {
  const body = Array.isArray(payload)
    ? { items: payload, total: payload.length, limit: 50, offset: 0 }
    : payload;
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
