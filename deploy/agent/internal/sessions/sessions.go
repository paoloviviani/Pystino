// Package sessions is the backend-agnostic materializer (PROTOCOL.md §7):
// it subscribes to a backend.Backend's event stream from process start,
// keeps a per-session transcript and ring buffer, enforces the text
// contract, gates auto-accept against machine policy, and answers
// session.sync.
package sessions

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"

	"pystino-agent/internal/backend"
	"pystino-agent/internal/policy"
)

// ringCapacity is how many envelopes each session's ring buffer keeps
// (PROTOCOL.md §7 gives "e.g. last 2000").
const ringCapacity = 2000

// ErrUnknownSession is returned by any method keyed on a sessionID the
// materializer has never Tracked or created.
var ErrUnknownSession = errors.New("sessions: unknown session")

// ErrAutoAcceptForbidden is returned by SetAutoAccept when the machine
// policy denies auto-accept outright — the machine's veto (PROTOCOL.md
// §4), never overridable from the link.
var ErrAutoAcceptForbidden = errors.New("sessions: auto-accept is denied by machine policy")

// Envelope is one pushed event, addressed by (epoch, seq) per session
// (PROTOCOL.md §5 "event" frame, §7).
type Envelope struct {
	SessionID string
	Epoch     string
	Seq       int64
	Event     backend.Event
}

// SyncResult is session.sync's answer: either Events (a contiguous tail
// starting at afterSeq+1, when the epoch matches and the ring still holds
// it) or Snapshot (everything else — no prior epoch, an epoch mismatch, or
// a ring gap).
type SyncResult struct {
	Epoch    string
	Seq      int64
	Events   []Envelope
	Snapshot *backend.Transcript
}

// sessionState is the materializer's per-session working state: the
// reconstructed transcript (messages, parts, permissions, status, usage,
// todos) plus the ring buffer and seq counter events are assigned from.
type sessionState struct {
	sessionID    string
	workspaceDir string
	seeded       bool
	autoAccept   bool

	seq  int64
	ring []Envelope

	messageOrder []string
	messages     map[string]*backend.Message
	partOrder    map[string][]string
	parts        map[string]map[string]*backend.Part

	permissionOrder []string
	permissions     map[string]*backend.PermissionRequest

	status backend.SessionStatus
	usage  *backend.Usage
	todos  []backend.Todo
}

func newSessionState(workspaceDir, sessionID string) *sessionState {
	return &sessionState{
		sessionID:    sessionID,
		workspaceDir: workspaceDir,
		messages:     map[string]*backend.Message{},
		partOrder:    map[string][]string{},
		parts:        map[string]map[string]*backend.Part{},
		permissions:  map[string]*backend.PermissionRequest{},
	}
}

// since returns the envelopes after afterSeq, and whether the ring buffer
// still holds all of them. afterSeq == s.seq (already up to date) always
// succeeds with zero events, even on a session whose ring is empty.
func (s *sessionState) since(afterSeq int64) ([]Envelope, bool) {
	if afterSeq == s.seq {
		return []Envelope{}, true
	}
	if afterSeq > s.seq {
		return nil, false
	}
	if len(s.ring) == 0 {
		return nil, false
	}
	firstSeq := s.ring[0].Seq
	if afterSeq+1 < firstSeq {
		return nil, false // trimmed past what the caller needs
	}
	idx := int(afterSeq + 1 - firstSeq)
	if idx < 0 || idx > len(s.ring) {
		return nil, false
	}
	out := make([]Envelope, len(s.ring)-idx)
	copy(out, s.ring[idx:])
	return out, true
}

// snapshot reconstructs the current Transcript from tracked state.
func (s *sessionState) snapshot() backend.Transcript {
	entries := make([]backend.TranscriptEntry, 0, len(s.messageOrder))
	for _, mid := range s.messageOrder {
		msg, ok := s.messages[mid]
		if !ok {
			continue
		}
		var parts []backend.Part
		for _, pid := range s.partOrder[mid] {
			if p, ok := s.parts[mid][pid]; ok {
				parts = append(parts, *p)
			}
		}
		entries = append(entries, backend.TranscriptEntry{Message: *msg, Parts: parts})
	}
	perms := make([]backend.PermissionRequest, 0, len(s.permissionOrder))
	for _, pid := range s.permissionOrder {
		if p, ok := s.permissions[pid]; ok {
			perms = append(perms, *p)
		}
	}
	return backend.Transcript{
		Messages:    entries,
		Permissions: perms,
		Status:      s.status,
		Usage:       s.usage,
		Todos:       s.todos,
	}
}

// Materializer is the whole per-machine event pipeline: one per running
// agent, wrapping exactly one backend.Backend.
type Materializer struct {
	back   backend.Backend
	policy policy.Policy
	epoch  string

	mu       sync.Mutex
	sessions map[string]*sessionState

	outCh chan Envelope
}

// New builds a Materializer over b, gated by pol (read once at
// construction — a policy change requires a restart, same as any other
// enroll-time setting).
func New(b backend.Backend, pol policy.Policy) *Materializer {
	epoch, err := randomEpoch()
	if err != nil {
		// crypto/rand failing means the machine's entropy source is broken,
		// which every other secret-minting call in this program also
		// depends on; there is no degraded mode worth offering.
		panic(fmt.Sprintf("sessions: minting epoch: %v", err))
	}
	return &Materializer{
		back:     b,
		policy:   pol,
		epoch:    epoch,
		sessions: map[string]*sessionState{},
		outCh:    make(chan Envelope, 4096),
	}
}

func randomEpoch() (string, error) {
	raw := make([]byte, 8)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

// Epoch is the process's own epoch, minted once at New and constant for
// the process's lifetime (PROTOCOL.md §7): a restart always mints a new
// one, which is precisely the signal that tells a client its cursor is
// stale and it must resync from a snapshot.
func (m *Materializer) Epoch() string { return m.epoch }

// Events is the live outward stream: every envelope this materializer
// emits, across every session, in emission order. Reading it is a
// best-effort convenience (a very slow or absent reader can miss live
// pushes) — session.sync's ring buffer and snapshot path are what
// guarantee no event is ever lost to a client that asks.
func (m *Materializer) Events() <-chan Envelope { return m.outCh }

// Track registers a session the materializer did not create itself — one
// discovered via the backend's own listing (e.g. at startup, or a subagent
// spawned as a "subtask" part) — so later event application and Sync know
// its workspace directory. A session already tracked is left alone: this
// must never clobber state built from live events.
func (m *Materializer) Track(workspaceDir string, sess backend.Session) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, exists := m.sessions[sess.ID]; exists {
		return
	}
	st := newSessionState(workspaceDir, sess.ID)
	st.status = sess.Status
	m.sessions[sess.ID] = st
}

// Start begins consuming the backend's event stream in a goroutine, until
// ctx is cancelled or the backend's subscription itself ends (the backend
// doc's "fatal, needs a new epoch" case — Start returns nothing to signal
// that beyond the goroutine simply stopping; a caller that needs to notice
// should watch the same ctx, or wrap this backend in one that surfaces the
// failure some other way).
func (m *Materializer) Start(ctx context.Context) error {
	ch, err := m.back.Subscribe(ctx)
	if err != nil {
		return fmt.Errorf("subscribing to backend: %w", err)
	}
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case be, ok := <-ch:
				if !ok {
					return
				}
				m.ApplyBackendEvent(ctx, be)
			}
		}
	}()
	return nil
}

// ApplyBackendEvent processes one raw backend event: translating it (the
// text contract, auto-accept interception), updating tracked state, and
// publishing whatever should reach a client. Exported so tests can drive it
// synchronously without a live goroutine or a real backend.
func (m *Materializer) ApplyBackendEvent(ctx context.Context, be backend.BackendEvent) {
	m.mu.Lock()
	st, exists := m.sessions[be.SessionID]
	if !exists {
		st = newSessionState(be.WorkspaceDir, be.SessionID)
		m.sessions[be.SessionID] = st
	} else if be.WorkspaceDir != "" {
		st.workspaceDir = be.WorkspaceDir
	}
	events, autoReply := m.translateLocked(st, be.Event)
	envs := make([]Envelope, 0, len(events))
	for _, ev := range events {
		envs = append(envs, m.appendRingLocked(st, ev))
	}
	m.mu.Unlock()

	for _, env := range envs {
		m.publish(env)
	}

	if autoReply != nil {
		err := m.back.ReplyPermission(ctx, st.workspaceDir, st.sessionID, autoReply.ID, backend.DecisionOnce, "")
		if err != nil {
			// The ask stays pending; a future permission.replied from the
			// backend (a human resolving it some other way) or a retried
			// auto-reply on the next asked event still resolves it. There is
			// no good way to surface this failure from inside an event
			// callback, and the alternative — leaving the tool blocked
			// forever with no attempt made — is worse.
			return
		}
		m.mu.Lock()
		env := m.appendRingLocked(st, backend.Event{
			Kind:      backend.EventPermissionReplied,
			RequestID: autoReply.ID,
			Decision:  backend.DecisionOnce,
			By:        "auto",
		})
		m.mu.Unlock()
		m.publish(env)
	}
}

// publish is best-effort (see Events's doc): a full channel drops the live
// push rather than blocking the event-processing goroutine, which would
// stall every session behind one slow reader.
func (m *Materializer) publish(env Envelope) {
	select {
	case m.outCh <- env:
	default:
	}
}

func (m *Materializer) appendRingLocked(st *sessionState, ev backend.Event) Envelope {
	st.seq++
	env := Envelope{SessionID: st.sessionID, Epoch: m.epoch, Seq: st.seq, Event: ev}
	st.ring = append(st.ring, env)
	if len(st.ring) > ringCapacity {
		trimmed := make([]Envelope, ringCapacity)
		copy(trimmed, st.ring[len(st.ring)-ringCapacity:])
		st.ring = trimmed
	}
	return env
}

// translateLocked applies one backend event to st, returning the event(s)
// to emit (usually zero or one) and, when a pending permission should be
// auto-replied, the request to reply to (handled by the caller outside the
// lock, since it means an I/O call to the backend). Caller holds m.mu.
func (m *Materializer) translateLocked(st *sessionState, ev backend.Event) ([]backend.Event, *backend.PermissionRequest) {
	switch ev.Kind {
	case backend.EventMessage:
		if ev.Message == nil {
			return nil, nil
		}
		if _, exists := st.messages[ev.Message.ID]; !exists {
			st.messageOrder = append(st.messageOrder, ev.Message.ID)
		}
		msg := *ev.Message
		st.messages[ev.Message.ID] = &msg
		return []backend.Event{ev}, nil

	case backend.EventPart:
		return m.translatePartLocked(st, ev)

	case backend.EventDelta:
		m.applyDeltaLocked(st, ev)
		return []backend.Event{ev}, nil

	case backend.EventPartRemoved:
		if parts, ok := st.parts[ev.MessageID]; ok {
			delete(parts, ev.PartID)
		}
		st.partOrder[ev.MessageID] = removeString(st.partOrder[ev.MessageID], ev.PartID)
		return []backend.Event{ev}, nil

	case backend.EventStatus:
		st.status = ev.Status
		return []backend.Event{ev}, nil

	case backend.EventPermissionAsked:
		if ev.Request == nil {
			return nil, nil
		}
		if st.autoAccept && m.policy.AutoAcceptAllowed() {
			req := *ev.Request
			return nil, &req
		}
		if _, exists := st.permissions[ev.Request.ID]; !exists {
			st.permissionOrder = append(st.permissionOrder, ev.Request.ID)
		}
		req := *ev.Request
		st.permissions[ev.Request.ID] = &req
		return []backend.Event{ev}, nil

	case backend.EventPermissionReplied:
		delete(st.permissions, ev.RequestID)
		st.permissionOrder = removeString(st.permissionOrder, ev.RequestID)
		return []backend.Event{ev}, nil

	case backend.EventUsage:
		st.usage = ev.Usage
		return []backend.Event{ev}, nil

	case backend.EventSession:
		return []backend.Event{ev}, nil

	case backend.EventError:
		return []backend.Event{ev}, nil

	case backend.EventTodo:
		st.todos = ev.Todos
		return []backend.Event{ev}, nil

	default:
		// Forward-compatible: an event kind this build doesn't know yet is
		// dropped rather than forwarded blind (PROTOCOL.md §5 says unknown
		// kinds are ignored by both sides).
		return nil, nil
	}
}

// translatePartLocked is the text contract (PROTOCOL.md §7): the first
// "part" event for a part id carries the text so far; a later "part" event
// for the same id is converted to a suffix delta when it only grew, dropped
// when it carries nothing new or has regressed, and re-baselined as a fresh
// upsert only when it is neither — never forwarded verbatim in a way that
// would contradict what was already sent.
func (m *Materializer) translatePartLocked(st *sessionState, ev backend.Event) ([]backend.Event, *backend.PermissionRequest) {
	if ev.Part == nil {
		return nil, nil
	}
	incoming := *ev.Part
	msgID, partID := incoming.MessageID, incoming.ID
	ensureMessageLocked(st, msgID, incoming.Role)

	parts, ok := st.parts[msgID]
	if !ok {
		parts = map[string]*backend.Part{}
		st.parts[msgID] = parts
	}
	existing, hasExisting := parts[partID]

	isText := incoming.Type == backend.PartText || incoming.Type == backend.PartReason
	if !hasExisting {
		st.partOrder[msgID] = append(st.partOrder[msgID], partID)
		stored := incoming
		parts[partID] = &stored
		return []backend.Event{ev}, nil
	}
	if !isText {
		stored := incoming
		parts[partID] = &stored
		return []backend.Event{ev}, nil
	}

	oldText, newText := existing.Text, incoming.Text
	switch {
	case newText == oldText:
		stored := incoming
		parts[partID] = &stored
		return nil, nil
	case strings.HasPrefix(newText, oldText):
		delta := newText[len(oldText):]
		stored := incoming
		parts[partID] = &stored
		return []backend.Event{{
			Kind:      backend.EventDelta,
			MessageID: msgID,
			PartID:    partID,
			Role:      incoming.Role,
			Field:     "text",
			Delta:     delta,
		}}, nil
	case strings.HasPrefix(oldText, newText):
		// A shorter resend of text already sent longer: no new information,
		// and forwarding it would regress what the client has assembled.
		return nil, nil
	default:
		// Genuinely different content under the same part id (the backend
		// replaced it) — re-baseline as a fresh full upsert rather than a
		// delta, which could never express a non-suffix change truthfully.
		stored := incoming
		parts[partID] = &stored
		return []backend.Event{ev}, nil
	}
}

// applyDeltaLocked folds a raw delta the backend itself emitted (as opposed
// to one this materializer synthesized in translatePartLocked) into the
// tracked part's text, creating a minimal placeholder part if this is
// somehow the first mention of it — defensive, since a well-behaved backend
// always sends a "part" event before any "delta" for the same id.
func (m *Materializer) applyDeltaLocked(st *sessionState, ev backend.Event) {
	ensureMessageLocked(st, ev.MessageID, ev.Role)
	parts, ok := st.parts[ev.MessageID]
	if !ok {
		parts = map[string]*backend.Part{}
		st.parts[ev.MessageID] = parts
	}
	p, ok := parts[ev.PartID]
	if !ok {
		p = &backend.Part{ID: ev.PartID, MessageID: ev.MessageID, Role: ev.Role, Type: backend.PartText}
		parts[ev.PartID] = p
		st.partOrder[ev.MessageID] = append(st.partOrder[ev.MessageID], ev.PartID)
	}
	p.Text += ev.Delta
}

// ensureMessageLocked registers msgID in message order with a placeholder
// Message if nothing has mentioned it yet. A backend that sends a part
// before (or without ever sending) a message-level event must not have its
// content silently dropped from the snapshot for want of metadata; a real
// EventMessage arriving later overwrites the placeholder in place, per the
// EventMessage case's own exists-check, without disturbing its position in
// messageOrder.
func ensureMessageLocked(st *sessionState, msgID, role string) {
	if _, exists := st.messages[msgID]; exists {
		return
	}
	st.messageOrder = append(st.messageOrder, msgID)
	st.messages[msgID] = &backend.Message{ID: msgID, Role: role}
}

func removeString(list []string, s string) []string {
	for i, v := range list {
		if v == s {
			return append(list[:i:i], list[i+1:]...)
		}
	}
	return list
}

// Sync implements session.sync (PROTOCOL.md §6): a contiguous tail when
// epoch matches and the ring still holds it, else a full snapshot. A
// session's transcript is seeded from the backend's own persisted record
// exactly once, lazily, the first time a snapshot is needed and no live
// event has already supplied it — cheap for the common case (a session
// this process has been watching the whole time already has everything).
func (m *Materializer) Sync(ctx context.Context, sessionID, epoch string, afterSeq int64) (SyncResult, error) {
	m.mu.Lock()
	st, ok := m.sessions[sessionID]
	if !ok {
		m.mu.Unlock()
		return SyncResult{}, ErrUnknownSession
	}
	if epoch == m.epoch {
		if events, ok := st.since(afterSeq); ok {
			seq := st.seq
			m.mu.Unlock()
			return SyncResult{Epoch: m.epoch, Seq: seq, Events: events}, nil
		}
	}
	needsSeed := !st.seeded
	workspaceDir := st.workspaceDir
	m.mu.Unlock()

	if needsSeed {
		tr, err := m.back.Transcript(ctx, workspaceDir, sessionID)
		if err != nil {
			return SyncResult{}, fmt.Errorf("fetching transcript for %s: %w", sessionID, err)
		}
		m.mu.Lock()
		mergeSeedLocked(st, tr)
		m.mu.Unlock()
	}

	m.mu.Lock()
	snap := st.snapshot()
	seq := st.seq
	m.mu.Unlock()
	return SyncResult{Epoch: m.epoch, Seq: seq, Snapshot: &snap}, nil
}

// mergeSeedLocked folds a backend's persisted transcript into st, keeping
// whatever st already has for any message/permission id live events have
// already supplied (those are more current than a point-in-time fetch that
// may have raced them). Seeded messages are ones this process has not seen
// a live event for, which — since the materializer subscribes from process
// start — means they predate this epoch; they are ordered before whatever
// live messages are already tracked, rather than appended after (seeding
// happens lazily, possibly well after those live messages arrived, so
// insertion order alone would put history after activity that happened
// later in wall-clock time). Caller holds m.mu.
func mergeSeedLocked(st *sessionState, tr backend.Transcript) {
	if st.seeded {
		return
	}
	var seededOrder []string
	for _, entry := range tr.Messages {
		if _, exists := st.messages[entry.Message.ID]; exists {
			continue
		}
		msg := entry.Message
		st.messages[msg.ID] = &msg
		seededOrder = append(seededOrder, msg.ID)
		parts := map[string]*backend.Part{}
		order := make([]string, 0, len(entry.Parts))
		for _, p := range entry.Parts {
			part := p
			parts[p.ID] = &part
			order = append(order, p.ID)
		}
		st.parts[msg.ID] = parts
		st.partOrder[msg.ID] = order
	}
	st.messageOrder = append(seededOrder, st.messageOrder...)
	for _, perm := range tr.Permissions {
		if _, exists := st.permissions[perm.ID]; exists {
			continue
		}
		req := perm
		st.permissions[perm.ID] = &req
		st.permissionOrder = append(st.permissionOrder, perm.ID)
	}
	if st.usage == nil {
		st.usage = tr.Usage
	}
	if len(st.todos) == 0 {
		st.todos = tr.Todos
	}
	if st.status == "" {
		st.status = tr.Status
	}
	st.seeded = true
}

// PendingPermissions is the count of permission requests currently awaiting
// a reply for sessionID — the Session.pendingPermissions field
// (PROTOCOL.md §6), which is the materializer's own aggregation, not
// anything a backend reports directly.
func (m *Materializer) PendingPermissions(sessionID string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, ok := m.sessions[sessionID]
	if !ok {
		return 0
	}
	return len(st.permissionOrder)
}

// AutoAccept reports whether auto-accept is currently on for sessionID.
func (m *Materializer) AutoAccept(sessionID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, ok := m.sessions[sessionID]
	return ok && st.autoAccept
}

// SetAutoAccept implements session.setAutoAccept (PROTOCOL.md §6): refused
// outright when the machine policy denies auto-accept, regardless of what
// Cerea asks for — the machine's veto, enforced here rather than trusted to
// whatever asked.
func (m *Materializer) SetAutoAccept(sessionID string, enabled bool) error {
	if enabled && !m.policy.AutoAcceptAllowed() {
		return ErrAutoAcceptForbidden
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	st, ok := m.sessions[sessionID]
	if !ok {
		return ErrUnknownSession
	}
	st.autoAccept = enabled
	return nil
}
