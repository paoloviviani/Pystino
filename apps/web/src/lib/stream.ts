/**
 * Reading a turn as it arrives.
 *
 * `EventSource` is the obvious tool and cannot be used: it only issues GET
 * requests, and a chat turn is a POST with a body. So this reads the response
 * body as a stream and parses SSE by hand — which is about forty lines, and the
 * alternative (a GET carrying the message in the query string) puts prompt text
 * in access logs and browser history, where it has no business being.
 *
 * The parser is deliberately strict about one thing: an event is only delivered
 * on a blank line. A chunk boundary can fall anywhere, including mid-word and
 * mid-JSON, so anything that assumes one read is one event drops text at
 * random and does it more often on a slow connection.
 */

import { BASE } from "./api";

export interface StreamHandlers {
  onDelta: (content: string) => void;
  onDone: (info: { request_id: string | null; usage: unknown; model: string | null }) => void;
  onError: (message: string) => void;
}

interface ParsedEvent {
  event: string;
  data: string;
}

function events(buffer: string): { events: ParsedEvent[]; rest: string } {
  const parsed: ParsedEvent[] = [];
  // Normalised because a proxy is free to rewrite line endings, and a parser
  // that only knows \n\n silently never fires behind one that uses \r\n.
  const normalised = buffer.replace(/\r\n/g, "\n");
  const blocks = normalised.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length > 0) parsed.push({ event, data: data.join("\n") });
  }
  return { events: parsed, rest };
}

export async function streamTurn(
  conversationId: string,
  body: { content: string; model?: string },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}/api/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
    signal,
  });

  if (!response.ok || !response.body) {
    let message = `The message could not be sent (${response.status}).`;
    try {
      const parsed = (await response.json()) as { detail?: string; error?: { message?: string } };
      message = parsed.error?.message ?? parsed.detail ?? message;
    } catch {
      /* the body was not JSON; the status is all there is to say */
    }
    handlers.onError(message);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    // `stream: true`, because a multi-byte character can straddle a chunk
    // boundary and decoding without it replaces the halves with U+FFFD. In
    // practice: accented text and emoji corrupting at random intervals.
    buffer += decoder.decode(value, { stream: true });
    const { events: batch, rest } = events(buffer);
    buffer = rest;

    for (const item of batch) {
      if (item.event === "delta") {
        const payload = JSON.parse(item.data) as { content?: string };
        if (payload.content) handlers.onDelta(payload.content);
      } else if (item.event === "error") {
        const payload = JSON.parse(item.data) as { message?: string };
        handlers.onError(payload.message ?? "The model could not answer.");
      } else if (item.event === "done") {
        handlers.onDone(
          JSON.parse(item.data) as {
            request_id: string | null;
            usage: unknown;
            model: string | null;
          },
        );
      }
    }
  }
}
