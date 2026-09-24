package opencode

import (
	"encoding/json"

	"pystino-agent/internal/fsutil"
)

// Mode and model are per-prompt in opencode (there is no server-side memory
// of a session's chosen mode/model between prompts), so this backend keeps
// its own overlay and passes it on every prompt_async call. Persisted to
// OverlayPath (if set) so a choice survives this agent restarting, even
// though opencode itself has no idea it existed.

func (b *Backend) getOverlay(sessionID string) sessionOverlay {
	b.overlayMu.Lock()
	defer b.overlayMu.Unlock()
	return b.overlay[sessionID]
}

func (b *Backend) setOverlay(sessionID string, o sessionOverlay) error {
	b.overlayMu.Lock()
	b.overlay[sessionID] = o
	b.overlayMu.Unlock()
	return b.saveOverlay()
}

func (b *Backend) loadOverlay() error {
	if b.cfg.OverlayPath == "" {
		return nil
	}
	body, err := fsutil.ReadFileOrEmpty(b.cfg.OverlayPath)
	if err != nil {
		return err
	}
	if body == nil {
		return nil
	}
	var m map[string]sessionOverlay
	if err := json.Unmarshal(body, &m); err != nil {
		return err
	}
	b.overlayMu.Lock()
	b.overlay = m
	b.overlayMu.Unlock()
	return nil
}

func (b *Backend) saveOverlay() error {
	if b.cfg.OverlayPath == "" {
		return nil
	}
	b.overlayMu.Lock()
	body, err := json.MarshalIndent(b.overlay, "", "  ")
	b.overlayMu.Unlock()
	if err != nil {
		return err
	}
	return fsutil.WriteFileAtomic(b.cfg.OverlayPath, append(body, '\n'), 0o600)
}
