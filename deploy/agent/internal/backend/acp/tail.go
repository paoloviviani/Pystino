package acp

import "sync"

// tailBuffer keeps the last n bytes written to it — enough to put an ACP
// agent's last gasp on stderr into an error message when it exits before
// ever becoming ready, without letting a noisy agent grow the buffer
// unboundedly over a long run.
type tailBuffer struct {
	mu  sync.Mutex
	buf []byte
	cap int
}

func newTailBuffer(cap int) *tailBuffer {
	return &tailBuffer{cap: cap}
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.cap {
		t.buf = t.buf[len(t.buf)-t.cap:]
	}
	return len(p), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
