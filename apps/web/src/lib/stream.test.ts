/**
 * The SSE reader.
 *
 * These tests exist because the failure mode is silent. A parser that assumes
 * one network read is one event does not throw; it drops text, and it drops
 * more of it on a slower connection than on the developer's machine.
 */

import { describe, expect, it, vi } from "vitest";

import { streamTurn } from "./stream";

function bodyOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

function respondWith(chunks: string[], status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      status === 200
        ? new Response(bodyOf(chunks), { status })
        : new Response(JSON.stringify({ error: { message: "Over budget." } }), { status }),
    ),
  );
}

function handlers() {
  const deltas: string[] = [];
  const reasoning: string[] = [];
  const errors: string[] = [];
  let done: unknown = null;
  return {
    deltas,
    errors,
    get done() {
      return done;
    },
    reasoning,
    spy: {
      onDelta: (piece: string) => deltas.push(piece),
      onReasoning: (piece: string) => reasoning.push(piece),
      onError: (message: string) => errors.push(message),
      onDone: (info: unknown) => {
        done = info;
      },
    },
  };
}

describe("streamTurn", () => {
  it("delivers deltas in order and finishes on done", async () => {
    respondWith([
      'event: delta\ndata: {"content":"Hel"}\n\n',
      'event: delta\ndata: {"content":"lo"}\n\n',
      'event: done\ndata: {"request_id":"req-1","usage":null,"model":"m"}\n\n',
    ]);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.deltas.join("")).toBe("Hello");
    expect(sink.done).toMatchObject({ request_id: "req-1" });
  });

  it("survives an event split across chunk boundaries", async () => {
    // The reason this file exists. A chunk boundary can fall anywhere,
    // including inside the JSON, and nothing about it is unusual.
    respondWith([
      'event: delta\ndata: {"cont',
      'ent":"split"}\n',
      "\nevent: done\ndata: {}\n\n",
    ]);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.deltas).toEqual(["split"]);
  });

  it("handles CRLF line endings", async () => {
    // A proxy is free to rewrite them, and a parser that only knows \n\n
    // silently never fires behind one that does.
    respondWith(['event: delta\r\ndata: {"content":"ok"}\r\n\r\n']);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.deltas).toEqual(["ok"]);
  });

  it("delivers reasoning separately from the answer", async () => {
    // Both arrive on the same stream and must not be concatenated: the whole
    // point of the collapsible section is that thinking is not the answer.
    respondWith([
      'event: reasoning\ndata: {"content":"hmm"}\n\n',
      'event: delta\ndata: {"content":"answer"}\n\n',
    ]);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.reasoning).toEqual(["hmm"]);
    expect(sink.deltas).toEqual(["answer"]);
  });

  it("returns quietly when the reader is aborted", async () => {
    // The stop button. Not a failure — the server keeps what was generated and
    // marks the row interrupted, so reporting an error here would show a
    // problem where the reader made a choice.
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            new ReadableStream({
              start(c) {
                c.enqueue(new TextEncoder().encode('event: delta\ndata: {"content":"a"}\n\n'));
                controller.abort();
                c.error(new DOMException("aborted", "AbortError"));
              },
            }),
            { status: 200 },
          ),
      ),
    );
    const sink = handlers();
    await expect(
      streamTurn("c1", { content: "hi" }, sink.spy, controller.signal),
    ).resolves.toBeUndefined();
    expect(sink.errors).toEqual([]);
  });

  it("reports an error event as an error", async () => {
    respondWith(['event: error\ndata: {"message":"You have exceeded your budget."}\n\n']);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.errors).toEqual(["You have exceeded your budget."]);
  });

  it("reports a non-200 in the server's own words", async () => {
    respondWith([], 429);
    const sink = handlers();
    await streamTurn("c1", { content: "hi" }, sink.spy);
    expect(sink.errors).toEqual(["Over budget."]);
  });
});
