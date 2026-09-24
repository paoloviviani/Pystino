package acp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"sync"
)

// rpcMessage is one line of ACP's wire format: JSON-RPC 2.0, newline
// delimited over stdio (PROTOCOL.md §2). A single Go type covers requests,
// responses and notifications in both directions — ACP has no method that
// needs more than these fields, and Method/ID's presence or absence is what
// discriminates the three (see readLoop).
type rpcMessage struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *rpcError) Error() string {
	return fmt.Sprintf("acp: rpc error %d: %s", e.Code, e.Message)
}

// rpcConn is one JSON-RPC 2.0 connection over a pair of stdio streams.
// Both sides of an ACP connection can send requests, responses and
// notifications, so this is not a client or a server: onRequest/onNotify
// are ACP's agent->client direction (session/request_permission,
// session/update), while call/notify are the client->agent direction
// (initialize, session/new, session/prompt, session/cancel, ...).
//
// readLoop dispatches every incoming line synchronously, in order, on its
// own goroutine: onRequest and onNotify must not block, because a response
// to one of our own pending calls can be the very next line, and a reader
// blocked inside a handler would stall that response (and every line after
// it) — this is also what lets Transcript's session/load rely on every
// replayed session/update having already updated session state by the time
// the load call's own response arrives (they are lines the same reader saw
// first).
type rpcConn struct {
	w   io.Writer
	wMu sync.Mutex

	mu      sync.Mutex
	nextID  int64
	pending map[string]chan rpcMessage

	onRequest func(id json.RawMessage, method string, params json.RawMessage)
	onNotify  func(method string, params json.RawMessage)
	logf      func(format string, args ...any)

	done chan struct{}
	err  error
}

func newRPCConn(w io.Writer, onRequest func(id json.RawMessage, method string, params json.RawMessage), onNotify func(method string, params json.RawMessage), logf func(format string, args ...any)) *rpcConn {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &rpcConn{
		w:         w,
		pending:   map[string]chan rpcMessage{},
		onRequest: onRequest,
		onNotify:  onNotify,
		logf:      logf,
		done:      make(chan struct{}),
	}
}

// readLoop consumes r until EOF or a read error, dispatching every message.
// It returns (and closes c.done) when the stream ends — the caller
// (runOnce) treats that as "the child process's stdout closed", the signal
// to reap the process and, unless Stop was called, restart it.
func (c *rpcConn) readLoop(r io.Reader) {
	defer close(c.done)
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), 16<<20)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var msg rpcMessage
		if err := json.Unmarshal(line, &msg); err != nil {
			c.logf("acp: bad line from agent: %v", err)
			continue
		}
		switch {
		case msg.Method != "" && len(msg.ID) > 0:
			c.onRequest(msg.ID, msg.Method, msg.Params)
		case msg.Method != "":
			c.onNotify(msg.Method, msg.Params)
		case len(msg.ID) > 0:
			c.deliver(msg)
		}
	}
	c.err = scanner.Err()
	c.failPending(c.err)
}

func (c *rpcConn) deliver(msg rpcMessage) {
	key := string(msg.ID)
	c.mu.Lock()
	ch, ok := c.pending[key]
	if ok {
		delete(c.pending, key)
	}
	c.mu.Unlock()
	if ok {
		ch <- msg
	}
}

func (c *rpcConn) failPending(err error) {
	if err == nil {
		err = io.ErrClosedPipe
	}
	c.mu.Lock()
	pending := c.pending
	c.pending = map[string]chan rpcMessage{}
	c.mu.Unlock()
	for _, ch := range pending {
		ch <- rpcMessage{Error: &rpcError{Code: -32000, Message: err.Error()}}
	}
}

func (c *rpcConn) registerPending() (string, chan rpcMessage) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextID++
	id := strconv.FormatInt(c.nextID, 10)
	ch := make(chan rpcMessage, 1)
	c.pending[id] = ch
	return id, ch
}

func (c *rpcConn) cancelPending(id string) {
	c.mu.Lock()
	delete(c.pending, id)
	c.mu.Unlock()
}

func (c *rpcConn) send(msg rpcMessage) error {
	msg.JSONRPC = "2.0"
	body, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	c.wMu.Lock()
	defer c.wMu.Unlock()
	if _, err := c.w.Write(append(body, '\n')); err != nil {
		return fmt.Errorf("acp: writing to agent: %w", err)
	}
	return nil
}

func marshalParams(params any) json.RawMessage {
	if params == nil {
		return nil
	}
	body, err := json.Marshal(params)
	if err != nil {
		return nil
	}
	return body
}

// call sends a request and blocks for its matching response, or until ctx
// is done (in which case the pending entry is cleaned up so a late response
// does not leak).
func (c *rpcConn) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	id, ch := c.registerPending()
	if err := c.send(rpcMessage{ID: json.RawMessage(id), Method: method, Params: marshalParams(params)}); err != nil {
		c.cancelPending(id)
		return nil, err
	}
	select {
	case msg := <-ch:
		if msg.Error != nil {
			return nil, msg.Error
		}
		return msg.Result, nil
	case <-ctx.Done():
		c.cancelPending(id)
		return nil, ctx.Err()
	case <-c.done:
		return nil, fmt.Errorf("acp: connection closed: %w", c.err)
	}
}

// callAsync sends a request without waiting for the response, returning a
// channel the caller can receive the eventual response from whenever it
// likes. Used by Prompt, which must return once session/prompt is sent
// (PROTOCOL.md/the task: async, like opencode's prompt_async) rather than
// block until the turn ends.
func (c *rpcConn) callAsync(method string, params any) (<-chan rpcMessage, error) {
	id, ch := c.registerPending()
	if err := c.send(rpcMessage{ID: json.RawMessage(id), Method: method, Params: marshalParams(params)}); err != nil {
		c.cancelPending(id)
		return nil, err
	}
	return ch, nil
}

// notify sends a request with no id: ACP's session/cancel is exactly this
// (fire and forget, no response expected).
func (c *rpcConn) notify(method string, params any) error {
	return c.send(rpcMessage{Method: method, Params: marshalParams(params)})
}

// respond answers an incoming request (session/request_permission is the
// only one this client ever receives and answers; anything else gets
// refuse below).
func (c *rpcConn) respond(id json.RawMessage, result any, rpcErr *rpcError) error {
	msg := rpcMessage{ID: id}
	if rpcErr != nil {
		msg.Error = rpcErr
	} else {
		msg.Result = marshalParams(result)
		if msg.Result == nil {
			msg.Result = json.RawMessage("{}")
		}
	}
	return c.send(msg)
}

// refuse answers an incoming request we do not implement (fs/*, terminal/*
// — we advertise neither capability in initialize, so an agent calling them
// anyway gets a standard JSON-RPC "method not found").
func (c *rpcConn) refuse(id json.RawMessage, method string) error {
	return c.respond(id, nil, &rpcError{Code: -32601, Message: "method not found", Data: marshalParams(map[string]string{"method": method})})
}
